"""Assemble the training set for the moderation suggester from three sources.

Every row carries its provenance, because the three are not interchangeable and the retrain
gate needs to vary them independently:

``real``
    Human decisions from DomainSubmission. The only source the binary reject head trains on,
    and the only source anything is ever *evaluated* on.

``derived``
    Public blocklists. Available but **off by default**, and the measurements are why.

    Blocklist data can genuinely teach a class from nothing: holding every real SPAM label
    out of the reason head and teaching SPAM only from HaGeZi domains recovered F1 0.58
    against 0.75 for real labels, from a standing start of zero. What it cannot do is share a
    classifier with a class that has real labels. Adding derived OFFENSIVE rows costs the
    real SPAM class badly, and there is no safe dose - even 100 rows takes SPAM F1 from 0.839
    to 0.688, and 2000 takes it to 0.532 - because adult domains and SEO-spam domains look
    alike in name space and the two classes end up competing.

    For OFFENSIVE specifically that trade is not worth making, because the list the labels
    would come from is *also available at prediction time*: the blacklist snapshot is already
    loaded and answers membership with a binary search, so rules.py checks it directly and
    gets an exact answer instead of a 0.58-F1 guess. Derived data is for a class whose source
    cannot be consulted at prediction time, and we do not currently have one.

    Blanket augmentation of the *binary* head was measured too, and did not survive
    bootstrapping (cold-start PR-AUC 0.777 -> 0.821 with CIs [0.686, 0.855] and [0.719,
    0.896]), so it is not done either.

``seed``
    A small hand-written file covering concepts neither other source has - offensive content
    that is not adult content, the do-not-crawl cases, promotion of a paid product. It exists
    to cover concepts, not to move aggregate numbers.

Historic malformed submissions are excluded. 70 rows are ``null``, an IP address or
capitalised; migrations 0032/0033 fixed that at the API layer, so training on them would teach
the model to detect a problem that can no longer occur.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Iterable, Optional

import requests

from mwmbl.indexer.blacklist_providers import (
    AdultContentBlacklistProvider, HaGeZiBlacklistProvider)
from mwmbl.models import DomainEvidence, DomainSubmission
from mwmbl.moderation.evidence import page_texts
from mwmbl.moderation.features import ModerationExample
from mwmbl.utils import VALID_DOMAIN_REGEX

logger = getLogger(__name__)

REAL = "real"
DERIVED = "derived"
SEED = "seed"

SEED_LABELS_PATH = Path(__file__).resolve().parents[2] / "devdata" / "moderation_seed_labels.jsonl"

# Blocklist entries are dominated by generated subdomains and tumblr blogs, which look nothing
# like a hand-typed submission; training on those would teach the model about the blocklist's
# shape rather than about spam. Apex-only, plausible length, no odd characters.
MAX_DERIVED_DOMAIN_LENGTH = 40


@dataclass
class TrainingRow:
    domain: str
    rejected: bool
    reason: str          # "" for approvals
    source: str          # REAL | DERIVED | SEED
    page_texts: list[str]
    timestamp: Optional[str] = None
    # Who submitted it. Not a feature - the model deliberately does not read the submitter,
    # because a "trust this account" model is a reputation counter wearing a model's clothes.
    # It is here to *slice the evaluation*: submissions from an account with no track record
    # reject at 54% against 1% for an established one, and only the former needs a model at
    # all, so that is the slice the retrain gate reads. None for derived and seed rows, which
    # are training-only.
    submitter: Optional[str] = None

    def to_example(self) -> ModerationExample:
        return ModerationExample(self.domain, self.page_texts)


def is_trainable_domain(domain: str) -> bool:
    """Reject the historic malformed names that the API no longer accepts."""
    return (bool(domain)
            and domain == domain.lower()
            and VALID_DOMAIN_REGEX.fullmatch(domain) is not None)


def real_rows() -> list[TrainingRow]:
    """Decided submissions, joined to whatever page text has been crawled for them."""
    evidence_by_domain = {
        evidence.domain: evidence
        for evidence in DomainEvidence.objects.filter(state=DomainEvidence.State.READY)
    }

    rows = []
    submissions = (DomainSubmission.objects
                   .filter(status__in=("APPROVED", "REJECTED"))
                   .order_by("submitted_on"))
    for submission in submissions.iterator():
        if not is_trainable_domain(submission.name):
            continue
        rejected = submission.status == "REJECTED"
        rows.append(TrainingRow(
            domain=submission.name,
            rejected=rejected,
            reason=submission.rejection_reason if rejected else "",
            source=REAL,
            page_texts=page_texts(evidence_by_domain.get(submission.name)),
            timestamp=submission.submitted_on.isoformat() if submission.submitted_on else None,
            submitter=str(submission.submitted_by_id),
        ))
    return rows


def derived_rows(reasons_needing_data: Iterable[str], per_reason: int,
                 exclude: set[str], seed: int = 0) -> list[TrainingRow]:
    """Blocklist domains standing in for reason classes with too little real data.

    ``exclude`` must contain every domain that appears in the real data, in either split, so a
    derived row can never duplicate - or leak the label of - something we evaluate on.
    """
    sources = {
        "OFFENSIVE": AdultContentBlacklistProvider.URL,
        "SPAM": HaGeZiBlacklistProvider.HAGEZI_URLS["tif_mini"],
    }

    rows = []
    for reason in reasons_needing_data:
        url = sources.get(reason)
        if url is None:
            logger.warning("No derived data source for reason %s", reason)
            continue
        pool = [domain for domain in _fetch_apex_domains(url) if domain not in exclude]
        random.Random(seed).shuffle(pool)
        rows.extend(
            TrainingRow(domain=domain, rejected=True, reason=reason, source=DERIVED,
                        page_texts=[])
            for domain in pool[:per_reason])
        logger.info("Derived %d %s rows from %s", min(per_reason, len(pool)), reason, url)
    return rows


def seed_rows(path: Path = SEED_LABELS_PATH) -> list[TrainingRow]:
    """Hand-written examples covering concepts the other two sources miss."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        record = json.loads(line)
        rows.append(TrainingRow(
            domain=record["domain"],
            rejected=record["status"] == "REJECTED",
            reason=record.get("reason", ""),
            source=SEED,
            page_texts=[record["text"]] if record.get("text") else [],
        ))
    return rows


def _fetch_apex_domains(url: str) -> list[str]:
    """Apex-only entries of a blocklist, in hosts or plain-domain format."""
    response = requests.get(url, timeout=120)
    response.raise_for_status()

    domains = []
    for line in response.text.split("\n"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        domain = (parts[1] if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1")
                  else parts[0])
        labels = domain.split(".")
        if len(labels) != 2 or len(domain) > MAX_DERIVED_DOMAIN_LENGTH:
            continue
        if not domain.replace("-", "").replace(".", "").isalnum():
            continue
        domains.append(domain)
    return domains
