"""The learned half of a moderation suggestion.

Two heads over the same features: will a moderator reject this, and if so, why. Both are
linear models over TF-IDF, which is the right size for the data - 375 real rejections will
not train anything larger - and costs well under a millisecond per prediction.

Where this runs matters. Predictions are made by the enrichment background task and stored on
DomainEvidence, so the moderation queue is a plain indexed database read and no web worker
ever loads this module. A missing or slow artifact therefore cannot touch a moderator's
request; the worst case is a queue row that honestly says it has not been assessed yet.

The fitted artifact lives in Postgres (mwmbl.models.ModerationModelArtifact), not on disk. It
is written monthly by a retrain and read by every worker, and a container filesystem is
neither shared between workers nor kept across a deploy. The copy in ``artifacts/`` is the
warm start for a database that has none yet, and nothing writes to it.

Loading mirrors mwmbl.tinysearchengine.super_search_select.judge: a lazy singleton behind a
lock, memoized failure, and graceful degradation to rules-only when no artifact can be loaded
so a deploy without one keeps working.
"""
from __future__ import annotations

import io
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging import getLogger
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
from django.conf import settings
from django.utils import timezone

from mwmbl.models import ModerationModelArtifact
from mwmbl.moderation.features import Featuriser, ModerationExample
from mwmbl.moderation.rules import APPROVE, EvidenceItem, REJECT, decisive

logger = getLogger(__name__)

MODEL_FILENAME = "model.joblib"
METRICS_FILENAME = "metrics.json"

# How long a loaded model is served before the database is asked whether a retrain has
# published a newer one. A retrain happens in one worker process and every worker serves
# suggestions, so without this they would disagree until the next restart. The check is a
# single indexed row read, and a minute of staleness after a monthly retrain costs nothing.
MODEL_REFRESH_SECONDS = 60

# A reason class with no real decisions behind it must not present as though it had. The
# reason head learns whatever classes the training data contains (reason_head.classes_ is the
# authority); this caps the ones that are not grounded in moderator decisions.
#
# OFFENSIVE is the only one today: there is exactly one real labelled example, so what it
# knows comes from the handful of hand-written seed rows. The stronger signal for that class
# is not learned at all - rules.py checks blocklist membership directly, exactly, at
# prediction time. See mwmbl.moderation.training_data for why that beats training on the
# same lists.
REASON_CONFIDENCE_CAP = {"OFFENSIVE": 0.6}


@dataclass
class Suggestion:
    action: str                       # APPROVE | REJECT | UNSURE
    confidence: float
    reason: str = ""
    reason_confidence: float = 0.0
    reason_source: str = "model"      # model | derived | rule
    model_version: str = ""
    evidence: list[dict] = field(default_factory=list)

    @property
    def review_priority(self) -> float:
        """Sort key for the queue: what most needs a human, first.

        Highest for confident rejects (someone should look, and probably agree), next for
        genuinely uncertain rows (only a human can settle them), lowest for confident
        approvals. Sorting on confidence alone cannot express this, because confidence is
        high at both ends of the scale.
        """
        if self.action == REJECT.upper():
            return 1.0 + self.confidence
        if self.action == APPROVE.upper():
            return 1.0 - self.confidence
        return 1.0


class ModerationModel:
    """A fitted featuriser plus the two heads, pickled together as one artifact."""

    # Class-level default so an artifact pickled before training domains were recorded still
    # answers the question. Instances always set their own in __init__.
    train_domains: set = frozenset()

    def __init__(self, featuriser: Featuriser, reject_head, reason_head, version: str,
                 train_domains: Optional[set] = None):
        self.featuriser = featuriser
        self.reject_head = reject_head
        self.reason_head = reason_head
        self.version = version
        # What this model was fitted on, so a later retrain can exclude those rows when it
        # scores this model on its own held-out set. Without it, the incumbent is measured
        # partly on data it memorised and the comparison quietly favours whatever is deployed.
        # Empty for artifacts pickled before this was recorded; mwmbl.moderation.train reports
        # that rather than assuming either way.
        self.train_domains = train_domains or set()

    def predict(self, examples: list[ModerationExample]) -> list[tuple[float, str, float]]:
        """(reject probability, reason, reason probability) for each example, in one pass."""
        if not examples:
            return []
        features = self.featuriser.transform(examples)
        reject_probabilities = self.reject_head.predict_proba(features)[:, 1]

        reason_probabilities = self.reason_head.predict_proba(features)
        best = reason_probabilities.argmax(axis=1)
        reasons = self.reason_head.classes_[best]
        confidences = reason_probabilities[np.arange(len(examples)), best]

        return [
            (float(reject), str(reason), min(float(confidence),
                                             REASON_CONFIDENCE_CAP.get(str(reason), 1.0)))
            for reject, reason, confidence in zip(reject_probabilities, reasons, confidences)
        ]


def suggest(domain: str, page_texts: list[str], evidence_items: list[EvidenceItem],
            model: Optional[ModerationModel] = None) -> Suggestion:
    """Compose a suggestion from the deterministic checks and, where they are silent, the model.

    A decisive check always wins. "Homepage returns 404" is not a matter of opinion, and
    letting a probability override it would produce exactly the suggestions a moderator learns
    to distrust.
    """
    if model is None:
        model = get_model()

    decisive_item = decisive(evidence_items)
    if decisive_item is not None:
        return Suggestion(
            action=decisive_item.implies_action,
            confidence=decisive_item.implies_confidence,
            reason=decisive_item.implies_reason,
            reason_confidence=decisive_item.implies_confidence,
            reason_source="rule",
            model_version=model.version if model else "",
            evidence=[item.to_dict() for item in evidence_items],
        )

    if model is None:
        # Rules-only degradation: say we don't know rather than inventing a default.
        return Suggestion(action="UNSURE", confidence=0.0, model_version="",
                          evidence=[item.to_dict() for item in evidence_items])

    reject_probability, reason, reason_confidence = model.predict(
        [ModerationExample(domain, page_texts)])[0]

    if reject_probability >= settings.MODERATION_REJECT_THRESHOLD:
        action, confidence = "REJECT", reject_probability
    elif reject_probability <= settings.MODERATION_APPROVE_THRESHOLD:
        action, confidence = "APPROVE", 1.0 - reject_probability
    else:
        action, confidence = "UNSURE", 1.0 - abs(reject_probability - 0.5) * 2

    suggested_reason = reason if action == "REJECT" else ""
    return Suggestion(
        action=action,
        confidence=float(confidence),
        reason=suggested_reason,
        reason_confidence=reason_confidence if action == "REJECT" else 0.0,
        # Describes the reason actually being suggested, so a suggestion carrying no reason
        # is never labelled by whatever the reason head happened to rank first.
        reason_source="derived" if suggested_reason in REASON_CONFIDENCE_CAP else "model",
        model_version=model.version,
        evidence=[item.to_dict() for item in evidence_items],
    )


_model: Optional[ModerationModel] = None
_loaded_stamp: Optional[tuple[str, datetime]] = None
_checked_at: Optional[float] = None
_lock = threading.Lock()


def get_model() -> Optional[ModerationModel]:
    """The published model, reloaded when a retrain has published a newer one.

    None (until the published artifact changes) when nothing can be loaded, so a suggestion
    falls back to the deterministic checks rather than failing.
    """
    global _model, _loaded_stamp, _checked_at
    with _lock:
        if _checked_at is not None and time.monotonic() - _checked_at < MODEL_REFRESH_SECONDS:
            return _model
        published = _published_stamp()
        if _checked_at is None or published != _loaded_stamp:
            _model = _load(published[0] if published else None)
            _loaded_stamp = published
        _checked_at = time.monotonic()
    return _model


def load_published_model() -> Optional[ModerationModel]:
    """The artifact the workers are serving, for the retrain to compare itself against.

    Deliberately not :func:`get_model`, whose cache exists to keep the request path cheap: a
    retrain wants the row as it stands right now, and it wants it without disturbing what the
    rest of the process is serving.
    """
    published = _published_stamp()
    return _load(published[0] if published else None)


def publish(model: ModerationModel, metrics: dict) -> ModerationModelArtifact:
    """Store a trained model and its metrics as the artifact every worker will serve."""
    buffer = io.BytesIO()
    joblib.dump(model, buffer)
    # Versions are dated, so a second retrain on the same day replaces the first rather than
    # colliding on the unique version. created_on is refreshed along with the bytes: it is
    # what tells the other workers this row has changed, since the version they compare
    # against has not.
    artifact, _ = ModerationModelArtifact.objects.update_or_create(
        version=model.version,
        defaults={"model": buffer.getvalue(), "metrics": metrics,
                  "created_on": timezone.now()})
    reset_model_cache()
    return artifact


def reset_model_cache() -> None:
    """Drop the cached model so the next call reloads. Used after a retrain, and by tests."""
    global _model, _loaded_stamp, _checked_at
    with _lock:
        _model = None
        _loaded_stamp = None
        _checked_at = None


def _published_stamp() -> Optional[tuple[str, datetime]]:
    """Version and write time of the newest stored artifact, None to fall back to the bundled.

    The write time is part of the identity because versions are dated: a second retrain on the
    same day replaces that row's bytes under the same version, and a worker comparing versions
    alone would keep serving the superseded pickle until it restarted.
    """
    row = (ModerationModelArtifact.objects.order_by("-created_on")
           .values("version", "created_on").first())
    return (row["version"], row["created_on"]) if row else None


def is_compatible(model: ModerationModel) -> bool:
    """Whether a loaded artifact can actually score a domain with the code that loaded it.

    A featuriser is pickled whole, so a change to the feature set - one more shape column, a
    block that moved - leaves old artifacts describing a matrix this code no longer builds, and
    the heads raise on the width mismatch at *predict* time. In the enrichment task that means
    every domain fails to be scored rather than one, and the failure looks like a crawl problem
    rather than what it is.

    So loading ends with one probe prediction, and a model that cannot answer it is treated
    exactly like a model that could not be unpickled: dropped, with the deterministic checks
    carrying on alone. A probe rather than a version stamp because it catches whatever drifted,
    not only the drift somebody remembered to bump a number for.
    """
    try:
        model.predict([ModerationExample("example.com", [])])
    except Exception:
        logger.exception("Moderation model %s could not score a probe domain", model.version)
        return False
    return True


def _checked(model: Optional[ModerationModel], where: str) -> Optional[ModerationModel]:
    if model is None or is_compatible(model):
        return model
    logger.error("Moderation model %s from %s was fitted on a different feature set than this "
                 "code builds; ignoring it and falling back to the deterministic checks. "
                 "Retrain to publish a compatible artifact.", model.version, where)
    return None


def _load(version: Optional[str]) -> Optional[ModerationModel]:
    if version is None:
        return _load_bundled()
    try:
        artifact = ModerationModelArtifact.objects.get(version=version)
        model = joblib.load(io.BytesIO(bytes(artifact.model)))
    except Exception:
        # An artifact pickled by a different scikit-learn version is the realistic case, and
        # it must not take the enrichment task down with it: fall back to the model shipped
        # with this code, which was pickled by the dependencies this code is running.
        logger.exception("Failed to load moderation model %s; falling back to the bundled "
                         "artifact", version)
        return _load_bundled()
    logger.info("Domain moderation model %s loaded from the database", model.version)
    return _checked(model, "the database")


def _load_bundled() -> Optional[ModerationModel]:
    """The artifact shipped in the source tree, for a database with no retrain in it yet."""
    model_path = Path(settings.DOMAIN_MODERATION_MODEL_DIR) / MODEL_FILENAME
    if not model_path.exists():
        logger.warning(
            "Domain moderation model not found at %s and none published; suggestions will "
            "use the deterministic checks only", model_path)
        return None
    try:
        model = joblib.load(model_path)
    except Exception:
        logger.exception("Failed to load the domain moderation model from %s", model_path)
        return None
    logger.info("Domain moderation model %s loaded from %s", model.version, model_path)
    return _checked(model, str(model_path))


def load_metrics() -> dict:
    """The metrics of the published model, so the retrain gate can compare against them.

    Read from the same row as the model they describe: the gate's guarantee is that a
    candidate beats the artifact currently being served, and metrics stored anywhere else
    could be describing a different one.
    """
    row = ModerationModelArtifact.objects.order_by("-created_on").values("metrics").first()
    if row is not None:
        return row["metrics"]
    bundled = Path(settings.DOMAIN_MODERATION_MODEL_DIR) / METRICS_FILENAME
    return json.loads(bundled.read_text()) if bundled.exists() else {}
