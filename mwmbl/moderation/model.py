"""The learned half of a moderation suggestion.

Two heads over the same features: will a moderator reject this, and if so, why. Both are
linear models over TF-IDF, which is the right size for the data - 375 real rejections will
not train anything larger - and costs well under a millisecond per prediction.

Where this runs matters. Predictions are made by the enrichment background task and stored on
DomainEvidence, so the moderation queue is a plain indexed database read and no web worker
ever loads this module. A missing or slow artifact therefore cannot touch a moderator's
request; the worst case is a queue row that honestly says it has not been assessed yet.

Loading mirrors mwmbl.tinysearchengine.super_search_select.judge: a lazy singleton behind a
lock, memoized failure, and graceful degradation to rules-only when the artifact is absent so
a deploy without it keeps working.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Optional

import numpy as np
from django.conf import settings

from mwmbl.moderation.features import Featuriser, ModerationExample
from mwmbl.moderation.rules import APPROVE, EvidenceItem, REJECT, decisive

logger = getLogger(__name__)

MODEL_FILENAME = "model.joblib"
METRICS_FILENAME = "metrics.json"

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

    def __init__(self, featuriser: Featuriser, reject_head, reason_head, version: str):
        self.featuriser = featuriser
        self.reject_head = reject_head
        self.reason_head = reason_head
        self.version = version

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
_load_attempted = False
_lock = threading.Lock()


def get_model() -> Optional[ModerationModel]:
    """Lazily load the shared model; None (memoized) if the artifact is unavailable."""
    global _model, _load_attempted
    if _load_attempted:
        return _model
    with _lock:
        if _load_attempted:
            return _model
        _model = _load(Path(settings.DOMAIN_MODERATION_MODEL_DIR))
        _load_attempted = True
    return _model


def reset_model_cache() -> None:
    """Drop the cached model so the next call reloads. Used after a retrain, and by tests."""
    global _model, _load_attempted
    with _lock:
        _model = None
        _load_attempted = False


def _load(model_dir: Path) -> Optional[ModerationModel]:
    model_path = model_dir / MODEL_FILENAME
    if not model_path.exists():
        logger.warning(
            "Domain moderation model not found at %s; suggestions will use the deterministic "
            "checks only", model_path)
        return None
    try:
        import joblib
        model = joblib.load(model_path)
    except Exception:
        logger.exception("Failed to load the domain moderation model from %s", model_path)
        return None
    logger.info("Domain moderation model %s loaded from %s", model.version, model_path)
    return model


def load_metrics(model_dir: Optional[Path] = None) -> dict:
    """The metrics written alongside the artifact, so the retrain gate can compare against it."""
    path = Path(model_dir or settings.DOMAIN_MODERATION_MODEL_DIR) / METRICS_FILENAME
    if not path.exists():
        return {}
    return json.loads(path.read_text())
