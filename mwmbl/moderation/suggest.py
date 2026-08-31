"""Tie the checks and the model together, and put the answer where the queue can read it.

Three entry points, split by when they can be run:

:func:`refresh_suggestion` writes the cached suggestion onto a DomainEvidence row. It is
called by the enrichment task, the backfill command and the post-retrain rescore - never by a
request.

:func:`suggestion_for` reads that cached suggestion back and adds the evidence that would go
stale if it were cached: the submitter's record, and whether this domain has been decided
since. Both are indexed lookups.

:func:`annotate_queue` says the same thing in SQL, because the queue filters, orders and
paginates thousands of pending rows in the database and cannot call :func:`suggestion_for` to
decide which ones to return.

:func:`one_row_per_domain` and :func:`annotate_votes` are what turn that row-per-submission
queryset into the row-per-domain the moderator actually reviews. Both are SQL for the same
reason.
"""
from __future__ import annotations

from logging import getLogger
from typing import Iterable

from django.db.models import (
    Case, CharField, Count, Exists, F, FloatField, IntegerField, OuterRef, Q, Subquery, Value,
    When)
from django.db.models.functions import Coalesce, Substr

from mwmbl.models import DomainEvidence, DomainSubmission, SearchResultVote
from mwmbl.moderation import rules
from mwmbl.moderation.evidence import page_texts
from mwmbl.moderation.model import Suggestion, UNUSABLE_MODEL_REASONS, suggest

logger = getLogger(__name__)


def refresh_suggestion(evidence: DomainEvidence) -> DomainEvidence:
    """Recompute and store the suggestion for one already-crawled domain."""
    items = [rules.EvidenceItem(**item) for item in (evidence.evidence or [])]
    suggestion = suggest(evidence.domain, page_texts(evidence), items)

    evidence.suggested_action = suggestion.action
    evidence.suggested_reason = suggestion.reason
    evidence.confidence = suggestion.confidence
    evidence.reason_confidence = suggestion.reason_confidence
    evidence.reason_source = suggestion.reason_source
    evidence.model_version = suggestion.model_version
    evidence.evidence = suggestion.evidence
    evidence.save(update_fields=[
        "suggested_action", "suggested_reason", "confidence", "reason_confidence",
        "reason_source", "model_version", "evidence",
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
            reason_detail=rules.implied_detail(decisive_live),
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

    reason = evidence.suggested_reason if action == "REJECT" else ""
    source = evidence.reason_source or "model"
    # OTHER is the one reason that explains nothing on its own, and a decision may not carry
    # it without the sentence the submitter is shown. When a check decided it that sentence
    # comes from the check, which is what reason_source == "rule" means here; when the model
    # did there is none, and a suggestion no client can send back is not one to draw - so it
    # goes to the moderator as UNSURE instead. model.suggest already stops storing those; this
    # is for the rows stored before it did, and annotate_queue mirrors it so the queue filters
    # and orders on what is actually drawn.
    #
    # The stored reason is not taken as proof that a check is still behind it: reason_source
    # says one was when the row was written, and a check that stops being decisive - or stops
    # implying OTHER - leaves rows saying "rule" with nothing left to explain them until the
    # next rescore. rules.other_detail asks the evidence itself, as does the queue.
    detail = rules.other_detail(cached) if reason == "OTHER" and source == "rule" else ""
    if action == "REJECT" and reason in UNUSABLE_MODEL_REASONS and not detail:
        action, confidence, reason = "UNSURE", 0.0, ""

    return Suggestion(
        action=action,
        confidence=confidence,
        reason=reason,
        reason_detail=detail,
        # The reason's own confidence, not the rejection's. A model can be sure a domain
        # should go and barely have a view on why, and showing "SPAM (0.92)" when the reason
        # head scored 0.31 would present a guess as a finding.
        reason_confidence=(evidence.reason_confidence or 0.0) if action == "REJECT" else 0.0,
        reason_source=evidence.reason_source or "model",
        model_version=evidence.model_version,
        evidence=list(evidence.evidence or []) + [item.to_dict() for item in live],
    )


# --------------------------------------------------------------------------------------
# The same adjustment, in SQL.
#
# suggestion_for adjusts the stored suggestion in two ways that depend on other rows - an
# earlier decision on the same domain overrides it, and an approval is withheld from a
# submitter with no track record - so neither can be precomputed onto DomainEvidence. The
# queue still has to filter, order and paginate on the result across every pending submission,
# which has to happen in the database.
#
# So it is written twice, and test_queue_display_matches_suggestion_for holds the two
# together. Filtering on UNSURE must return exactly the rows drawn as UNSURE, and the "needs a
# human first" ordering must sort on the confidence the moderator is actually shown.

DECIDED = Q(status__in=("APPROVED", "REJECTED"))


def annotate_queue(submissions):
    """Annotate submissions with the suggestion each one will be rendered with.

    Adds ``displayed_action``, ``displayed_reason``, ``displayed_confidence`` and
    ``displayed_priority``, all NULL while the domain has no READY evidence - which is how
    "not assessed yet" sorts last and matches no suggestion filter.
    """
    # DomainEvidence is keyed on the domain rather than by a foreign key, because domains get
    # resubmitted and the crawl belongs to the domain. A correlated subquery joins them
    # without one, and keeps the filtering and ordering in SQL.
    evidence = DomainEvidence.objects.filter(domain=OuterRef("name"))
    submissions = submissions.annotate(
        # Prefixed because DomainSubmission already has suggested_status and suggested_reason
        # columns - the audit of what a moderator was shown - and an annotation may not
        # shadow a real field.
        evidence_state=Subquery(evidence.values("state")[:1]),
        evidence_action=Subquery(evidence.values("suggested_action")[:1]),
        evidence_reason=Subquery(evidence.values("suggested_reason")[:1]),
        evidence_confidence=Subquery(evidence.values("confidence")[:1]),
        evidence_source=Subquery(evidence.values("reason_source")[:1]),
        # The live checks, as one boolean each. Only PENDING submissions are ever queued, so
        # a submission never counts itself in either of these.
        prior_approved=Exists(DomainSubmission.objects.filter(
            name=OuterRef("name"), status="APPROVED")),
        prior_rejected=Exists(DomainSubmission.objects.filter(
            name=OuterRef("name"), status="REJECTED")),
        submitter_decided=Exists(DomainSubmission.objects.filter(
            DECIDED, submitted_by_id=OuterRef("submitted_by_id"))),
        # rules.other_detail, as far as SQL can ask it: is there a cached check implying
        # OTHER, and so a sentence to send with an OTHER rejection. A containment test rather
        # than a column comparison because it is a question about the evidence list, and
        # Exists rather than a lookup on the annotation above so it reads as the join it is.
        explained_by_a_check=Exists(DomainEvidence.objects.filter(
            domain=OuterRef("name"), evidence__contains=rules.IMPLIES_OTHER)),
    )

    scored = Q(evidence_state=DomainEvidence.State.READY)
    # A prior decision only overrides the stored suggestion when it is stronger than the check
    # that produced it. reason_source is how we know a check produced it at all: when it did,
    # the stored confidence *is* that check's implied confidence (see model.suggest).
    stored_check_holds = (Q(evidence_source="rule")
                          & Q(evidence_confidence__gte=rules.PRIOR_DECISION_CONFIDENCE))
    prior_approval = scored & Q(prior_approved=True) & ~stored_check_holds
    prior_rejection = (scored & Q(prior_rejected=True) & Q(prior_approved=False)
                       & ~stored_check_holds)
    # No cached check implies APPROVE - they are all reasons to reject - so a stored APPROVE
    # is always the model's, which is what the withheld-approval rule is about.
    withheld = scored & Q(evidence_action="APPROVE") & Q(submitter_decided=False)
    # suggestion_for's other downgrade: a stored REJECT nobody can send back - one whose
    # reason is OTHER with no check behind it, so there is no detail to send, or one carrying
    # no reason at all. Both halves of what suggestion_for asks: reason_source says a check
    # produced the reason, and the evidence still has to contain the check that explains it -
    # a stale row can say "rule" and have nothing implying OTHER left in its evidence.
    explained = Q(evidence_source="rule") & Q(explained_by_a_check=True)
    unexplained = scored & Q(evidence_action="REJECT") & (
        (Q(evidence_reason="OTHER") & ~explained)
        | Q(evidence_reason="") | Q(evidence_reason__isnull=True))

    submissions = submissions.annotate(
        displayed_action=Case(
            When(prior_approval, then=Value("APPROVE")),
            When(prior_rejection, then=Value("REJECT")),
            When(unexplained, then=Value("UNSURE")),
            When(withheld, then=Value("UNSURE")),
            When(scored, then=F("evidence_action")),
            default=Value(None),
            output_field=CharField(),
        ),
        displayed_confidence=Case(
            When(prior_approval | prior_rejection,
                 then=Value(rules.PRIOR_DECISION_CONFIDENCE)),
            When(unexplained, then=Value(0.0)),
            When(withheld, then=Value(0.0)),
            When(scored, then=F("evidence_confidence")),
            default=Value(None),
            output_field=FloatField(),
        ),
        displayed_reason=Case(
            When(prior_rejection, then=Value(rules.PRIOR_DECISION_REASON)),
            When(scored & Q(evidence_action="REJECT") & ~prior_approval & ~unexplained,
                 then=F("evidence_reason")),
            When(scored, then=Value("")),
            default=Value(None),
            output_field=CharField(),
        ),
    )

    # Suggestion.review_priority, over the adjusted values. This is why the sort key is not a
    # stored column: the rows whose approval was withheld are the ones a human most needs to
    # look at, and a precomputed priority would sort them by the confidence of the approval we
    # decided not to show - straight to the bottom of "what needs a human".
    return submissions.annotate(
        displayed_priority=Case(
            When(Q(displayed_action="REJECT"), then=Value(1.0) + F("displayed_confidence")),
            When(Q(displayed_action="APPROVE"), then=Value(1.0) - F("displayed_confidence")),
            When(Q(displayed_action="UNSURE"), then=Value(1.0)),
            default=Value(None),
            output_field=FloatField(),
        ),
    )


# --------------------------------------------------------------------------------------
# From a submission per row to a domain per row.


def one_row_per_domain(submissions, status: str | None = "PENDING", pick: str = "first"):
    """Keep one submission per name, and count the rest onto it as ``submission_count``.

    A moderator reviews a *domain*: nine submissions of cheap-rolex-outlet.biz are one card
    and one decision, not nine. Deliberately not a GROUP BY, though: the suggestion depends on
    the submitter's own track record (see :func:`suggestion_for`), and a domain submitted by
    two accounts has no single one to read. Collapsing to a representative row instead leaves
    :func:`annotate_queue` and :func:`suggestion_for` working on a submission, which is what
    holds those two together.

    ``pick`` chooses which row represents the domain, and in both cases it is the row whose
    own fields the client is going to draw:

    * ``"first"`` - the earliest submission, because the queue card says "first submitted 6
      days ago by anon_4417";
    * ``"last"`` - the most recently touched one, because a history listing shows the decision
      that currently stands.

    Ties break on the primary key, so the representative is deterministic either way.
    """
    touched = Coalesce("status_changed_on", "submitted_on")
    submissions = submissions.annotate(touched=touched)

    same_domain = DomainSubmission.objects.filter(name=OuterRef("name"))
    if status is not None:
        same_domain = same_domain.filter(status=status)

    if pick == "first":
        beats = same_domain.filter(
            Q(submitted_on__lt=OuterRef("submitted_on"))
            | Q(submitted_on=OuterRef("submitted_on"), pk__lt=OuterRef("pk")))
    else:
        beats = same_domain.annotate(touched=touched).filter(
            Q(touched__gt=OuterRef("touched"))
            | Q(touched=OuterRef("touched"), pk__gt=OuterRef("pk")))

    return submissions.annotate(
        submission_count=Coalesce(
            Subquery(same_domain.values("name").annotate(n=Count("pk")).values("n"),
                     output_field=IntegerField()),
            Value(0)),
    ).filter(~Exists(beats))


def annotate_votes(submissions):
    """Add ``upvotes`` and ``downvotes``: the votes cast on URLs belonging to the domain.

    In SQL because the queue orders on these across the whole backlog. SearchResultVote
    carries the host in its own indexed column for exactly this - Postgres cannot pick a
    domain out of a URLField - and both sides drop a leading ``www.`` so that a vote on
    www.example.com/x counts towards a submission of example.com.

    Coalesced to zero rather than left NULL: a domain nobody has voted on has no votes, and
    ordering on NULL would scatter those rows through the sort instead of ending it.
    """
    def counted(vote_type):
        return Coalesce(
            Subquery(SearchResultVote.objects
                     .filter(domain=OuterRef("bare_name"), vote_type=vote_type)
                     .values("domain").annotate(n=Count("pk")).values("n"),
                     output_field=IntegerField()),
            Value(0))

    return submissions.annotate(
        bare_name=Case(When(name__startswith="www.", then=Substr("name", 5)),
                       default=F("name"), output_field=CharField()),
    ).annotate(upvotes=counted("upvote"), downvotes=counted("downvote"))


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
