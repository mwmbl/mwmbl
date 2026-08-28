"""Tie the checks and the model together, and put the answer where the queue can read it.

Two entry points, split by when they can be run:

:func:`refresh_suggestion` writes the cached suggestion onto a DomainEvidence row. It is
called by the enrichment task, the backfill command and the post-retrain rescore - never by a
request.

:func:`suggestion_for` reads that cached suggestion back and adds the evidence that would go
stale if it were cached: the submitter's record, and whether this domain has been decided
since. Both are indexed lookups.
"""
from __future__ import annotations

from logging import getLogger
from typing import Iterable

from django.db.models import Count, Q

from mwmbl.models import DomainEvidence, DomainSubmission
from mwmbl.moderation import rules
from mwmbl.moderation.evidence import page_texts
from mwmbl.moderation.model import Suggestion, suggest

logger = getLogger(__name__)


def refresh_suggestion(evidence: DomainEvidence) -> DomainEvidence:
    """Recompute and store the suggestion for one already-crawled domain."""
    items = [rules.EvidenceItem(**item) for item in (evidence.evidence or [])]
    suggestion = suggest(evidence.domain, page_texts(evidence), items)

    evidence.suggested_action = suggestion.action
    evidence.suggested_reason = suggestion.reason
    evidence.confidence = suggestion.confidence
    evidence.review_priority = suggestion.review_priority
    evidence.model_version = suggestion.model_version
    evidence.evidence = suggestion.evidence
    evidence.save(update_fields=[
        "suggested_action", "suggested_reason", "confidence", "review_priority",
        "model_version", "evidence",
    ])
    return evidence


def suggestion_for(submission: DomainSubmission,
                   evidence: DomainEvidence | None,
                   submitter: dict | None = None,
                   prior: dict | None = None) -> Suggestion | None:
    """The stored suggestion, plus live evidence. None while the domain is still being crawled.

    Returning None rather than a default is deliberate: a queue row that says "not assessed
    yet" is honest, and a fabricated APPROVE at zero confidence is not.

    ``submitter`` and ``prior`` let a caller rendering many rows pass counts it has already
    aggregated, instead of paying two queries per row. See :func:`submitter_records` and
    :func:`prior_decision_counts`.
    """
    if evidence is None or evidence.state != DomainEvidence.State.READY:
        return None

    record = submitter if submitter is not None else submitter_record(submission.submitted_by_id)
    priors = prior if prior is not None else prior_decisions(submission)
    live = rules.live_evidence(record, priors)

    # A live check can be decisive even when the cached ones were not - most usefully "this
    # domain has already been approved before", which is true of 615 resubmitted domains.
    decisive_live = rules.decisive(live)
    cached = [rules.EvidenceItem(**item) for item in (evidence.evidence or [])]
    cached_decisive = rules.decisive(cached)
    if decisive_live is not None and (
            cached_decisive is None
            or decisive_live.implies_confidence > cached_decisive.implies_confidence):
        return Suggestion(
            action=decisive_live.implies_action,
            confidence=decisive_live.implies_confidence,
            reason=decisive_live.implies_reason,
            reason_confidence=decisive_live.implies_confidence,
            reason_source="rule",
            model_version=evidence.model_version,
            evidence=[item.to_dict() for item in cached] + [item.to_dict() for item in live],
        )

    action = evidence.suggested_action
    confidence = evidence.confidence or 0.0
    has_track_record = record["approved"] + record["rejected"] > 0
    if action == "APPROVE" and cached_decisive is None and not has_track_record:
        # Measured on held-out decisions: an APPROVE suggestion is wrong 44% of the time for a
        # submitter with no track record, and lowering the threshold does not rescue it (still
        # 28% wrong at 0.10). The cause is a prior shift, not a bad model - it is trained on a
        # population that rejects 11% and asked about one that rejects 54%, so a low reject
        # score means much less here than the number suggests. Rejections are unaffected
        # (precision 0.88 on this same slice), so only the approve side is withheld.
        action, confidence = "UNSURE", 0.0

    return Suggestion(
        action=action,
        confidence=confidence,
        reason=evidence.suggested_reason if action == "REJECT" else "",
        reason_confidence=confidence if action == "REJECT" else 0.0,
        reason_source="rule" if cached_decisive is not None else "model",
        model_version=evidence.model_version,
        evidence=list(evidence.evidence or []) + [item.to_dict() for item in live],
    )


def submitter_record(user_id: int) -> dict:
    """How this submitter's previous submissions were decided.

    Worth its own query: on the last year of decisions, a submitter with no track record was
    rejected 54% of the time against 1% for an established one, which is a bigger effect than
    anything the model reads off the domain name.
    """
    counts = (DomainSubmission.objects
              .filter(submitted_by_id=user_id)
              .aggregate(approved=Count("pk", filter=Q(status="APPROVED")),
                         rejected=Count("pk", filter=Q(status="REJECTED"))))
    return {"approved": counts["approved"] or 0, "rejected": counts["rejected"] or 0}


def prior_decisions(submission: DomainSubmission) -> dict:
    """How other submissions of the same domain were decided."""
    counts = (DomainSubmission.objects
              .filter(name=submission.name)
              .exclude(pk=submission.pk)
              .aggregate(approved=Count("pk", filter=Q(status="APPROVED")),
                         rejected=Count("pk", filter=Q(status="REJECTED"))))
    return {"approved": counts["approved"] or 0, "rejected": counts["rejected"] or 0}


def submitter_records(user_ids: Iterable[int]) -> dict[int, dict]:
    """Decision counts for several submitters in one query, for rendering a queue page."""
    counts = (DomainSubmission.objects
              .filter(submitted_by_id__in=list(user_ids))
              .values("submitted_by_id")
              .annotate(approved=Count("pk", filter=Q(status="APPROVED")),
                        rejected=Count("pk", filter=Q(status="REJECTED"))))
    return {row["submitted_by_id"]: {"approved": row["approved"], "rejected": row["rejected"]}
            for row in counts}


def prior_decision_counts(names: Iterable[str]) -> dict[str, dict]:
    """Decision counts per domain name, for rendering a queue page in one query.

    Counts every decided submission of the name, including - unlike :func:`prior_decisions` -
    the row being rendered. That is harmless here because the queue only ever shows PENDING
    submissions, which are by definition not counted.
    """
    counts = (DomainSubmission.objects
              .filter(name__in=list(names))
              .values("name")
              .annotate(approved=Count("pk", filter=Q(status="APPROVED")),
                        rejected=Count("pk", filter=Q(status="REJECTED"))))
    return {row["name"]: {"approved": row["approved"], "rejected": row["rejected"]}
            for row in counts}
