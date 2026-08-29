import logging

from allauth.account.adapter import get_adapter
from allauth.account.models import EmailConfirmationHMAC
from allauth.account.utils import setup_user_email, send_email_confirmation
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from ninja import Router
from ninja.pagination import paginate
from ninja_jwt.authentication import JWTAuth
from polar_sdk import Polar
from polar_sdk import models as polar_models
from polar_sdk.models import SubscriptionCancel
from polar_sdk.webhooks import validate_event, WebhookVerificationError

from mwmbl.exceptions import InvalidRequest
from mwmbl.search_auth import invalidate_api_key_cache, invalidate_user_api_key_cache
from mwmbl.utils import normalize_domain, validate_domain
from mwmbl import pricing
from mwmbl.background import enrich_domain_submission, stats_manager
from mwmbl.models import AgreementType, MwmblUser, DomainEvidence, DomainSubmission, SearchResultVote, ApiKey, UsageBucket, UserBilling, UserAgreement, MarketingConsent, MarketingSource, generate_username
from mwmbl.moderation.suggest import (
    annotate_queue, annotate_votes, one_row_per_domain, prior_decision_counts, prior_decisions,
    submitter_record, submitter_records, suggestion_for,
)
from mwmbl.signals import schedule_blacklist_rebuild
from mwmbl.platform.schemas import (
    Registration, ConfirmEmail, DomainSubmissionSchema, UpdateDomainSubmission,
    VoteRequest, VoteRemoveRequest, VoteStatsRequest, VoteResponse, VoteStats, UserVoteHistory,
    CreateApiKeyRequest, ApiKeyCreatedResponse, ApiKeyListItem,
    UserProfileResponse, SubscriptionResponse, CheckoutRequest, CheckoutResponse, UpdateSpendLimitRequest,
    ForgotPasswordRequest, ResetPasswordRequest,
    AgreementAcceptRequest, AgreementResponse,
    MarketingConsentRequest, MarketingConsentResponse, MarketingConsentListResponse,
    BulkDecisionRequest, ModeratedDomainSchema, ModerationHistory, ModerationQueue,
    QueueItemSchema, SubmissionDetailSchema,
)

logger = logging.getLogger(__name__)

router = Router(tags=["Platform"])


def check_email_verified(request):
    from_email_address = request.user.emailaddress_set.first()
    if not from_email_address.verified:
        raise InvalidRequest("Email address is not verified", status=403)


@router.post(
    '/register',
    summary="Register a new user",
    description=(
        "Create a new Mwmbl user account. Only `email` and `password` are required. "
        "`username` is optional — if omitted, a unique name is generated automatically in the "
        "form `adjective_noun_NNN` (e.g. `swift_falcon_379`). "
        "A confirmation email will be sent to the provided address; the account cannot be used "
        "until the email is confirmed via the `/platform/confirm-email` endpoint. "
        "The assigned username is returned in the response."
    ),
)
def register(request, registration: Registration):
    if MwmblUser.objects.filter(email=registration.email).exists():
        raise InvalidRequest("Email already registered")

    username = registration.username or generate_username()
    if registration.username and MwmblUser.objects.filter(username=registration.username).exists():
        raise InvalidRequest("Username already exists")

    user = MwmblUser(username=username, email=registration.email)
    user.set_password(registration.password)
    user.save()

    if registration.agreements:
        _record_agreements(user, registration.agreements)

    if registration.source is not None:
        _record_marketing_consent(user, registration.source, registration.marketing_opt_in)

    setup_user_email(request, user, [])
    send_email_confirmation(request, user, signup=True)

    return {
        "status": "ok",
        "username": username,
        "message": "User registered successfully. Check your email for confirmation."
    }


@router.post(
    "/confirm-email",
    summary="Confirm email address",
    description=(
        "Confirm a user's email address using the key sent in the confirmation email. "
        "Only `email` and `key` are required. The `username` field is accepted for backwards "
        "compatibility but is ignored. "
        "The confirmed account's username is returned in the response."
    ),
)
def confirm_email(request, confirm: ConfirmEmail):
    confirmation = EmailConfirmationHMAC.from_key(confirm.key)
    if confirmation is None:
        raise InvalidRequest("Invalid confirmation key")

    if confirmation.email_address.email != confirm.email:
        raise InvalidRequest("Invalid email or key")

    adapter = get_adapter()
    adapter.confirm_email(request, confirmation.email_address)

    return {
        "status": "ok",
        "username": confirmation.email_address.user.username,
        "message": "Email confirmed successfully."
    }


@router.get(
    "/protected",
    auth=JWTAuth(),
    summary="Test authentication",
    description=(
        "A simple endpoint to verify that your JWT token is valid and your email is confirmed. "
        "Returns a success message if authenticated."
    ),
)
def protected(request):
    check_email_verified(request)
    return {"status": "ok", "message": "You are authenticated!"}


@router.delete(
    "/users/{username}",
    auth=JWTAuth(),
    summary="Delete user account",
    description=(
        "Permanently delete the authenticated user's account. "
        "Users can only delete their own account. This action is irreversible."
    ),
)
def delete_user(request, username: str):
    user = MwmblUser.objects.get(username=username)
    if user is None:
        raise InvalidRequest("User not found.", status=404)

    if user != request.user:
        raise InvalidRequest("You can only delete your own account.")

    invalidate_user_api_key_cache(user.id)
    user.delete()
    return {"status": "ok", "message": "User deleted."}


@router.get(
    "/domain-submissions/domains/{domain}",
    response=list[DomainSubmissionSchema],
    summary="Get submissions for a domain",
    description=(
        "Retrieve all domain submissions for a specific domain name. "
        "Results are paginated. Use `limit` and `offset` query parameters to page through results."
    ),
)
@paginate
def get_domain_submissions_for_domain(request, domain: str) -> list[DomainSubmissionSchema]:
    # Submissions are stored under the normalized domain, so look them up the same way, otherwise a
    # client cannot find back a submission it made using the URL it submitted.
    return DomainSubmission.objects.filter(name=normalize_domain(domain)).all()


@router.get(
    "/domain-submissions",
    response=list[DomainSubmissionSchema],
    summary="List all domain submissions",
    description=(
        "Retrieve all domain submissions across all users. "
        "Results are paginated. Use `limit` and `offset` query parameters to page through results."
    ),
)
@paginate
def get_domain_submissions(request) -> list[DomainSubmission]:
    return DomainSubmission.objects.all()


@router.post(
    "/domain-submissions/",
    auth=JWTAuth(),
    summary="Submit a domain for crawling",
    description=(
        "Submit a domain name to be considered for inclusion in the Mwmbl crawl queue. "
        "Submissions are reviewed before the domain is added. "
        "Requires a verified account."
    ),
)
def submit_domain(request, domain: str):
    check_email_verified(request)
    try:
        validate_domain(domain)
    except ValidationError:
        raise InvalidRequest(f"Invalid domain: {domain}")

    submission = DomainSubmission(name=normalize_domain(domain), submitted_by=request.user)
    submission.save()
    return {"status": "ok", "message": "Domain submitted for review."}


@router.delete(
    "/domain-submissions/ids/{submission_id}",
    auth=JWTAuth(),
    summary="Delete a domain submission",
    description=(
        "Delete a domain submission by its ID. "
        "Users can only delete their own submissions. "
        "Requires a verified account."
    ),
)
def delete_submission(request, submission_id: int):
    check_email_verified(request)
    submission = DomainSubmission.objects.get(id=submission_id)
    if submission is None:
        raise InvalidRequest("Submission not found.", status=404)

    if request.user != submission.submitted_by:
        raise InvalidRequest("You can only delete your own submissions.")

    submission.delete()
    return {"status": "ok", "message": "Submission deleted."}


@router.post(
    "/domain-submissions/ids/{submission_id}",
    auth=JWTAuth(),
    summary="Update a domain submission status",
    description=(
        "Update the status of a domain submission (e.g. approve or reject it). "
        "Requires the `change_domain_submission_status` permission. "
        "Requires a verified account."
    ),
)
def update_submission_status(request, submission_id: int, update_submission: UpdateDomainSubmission):
    check_email_verified(request)
    submission = DomainSubmission.objects.get(id=submission_id)
    if submission is None:
        raise InvalidRequest("Submission not found.", status=404)

    if not request.user.has_perm("mwmbl.change_domain_submission_status"):
        raise InvalidRequest("You do not have permission to update this submission.")

    apply_decision(submission, update_submission, request.user)
    return {"status": "ok", "message": "Submission updated."}


def apply_decision(submission: DomainSubmission, decision, user) -> None:
    """Record a moderator's decision, along with the suggestion they were shown.

    The suggestion fields are an audit trail, not a copy of the live one on DomainEvidence: a
    retrain rewrites that row, and we need to keep what was actually on screen so the effect
    of the suggestions on decisions stays measurable.
    """
    submission.status = decision.status
    submission.rejection_reason = decision.rejection_reason
    submission.rejection_detail = decision.rejection_detail
    submission.status_changed_by = user
    submission.status_changed_on = timezone.now()
    submission.suggested_status = decision.suggested_status or ""
    submission.suggested_reason = decision.suggested_reason or ""
    submission.suggestion_confidence = decision.suggestion_confidence
    submission.suggestion_model_version = decision.suggestion_model_version or ""
    submission.save()


def check_moderator(request):
    if not request.user.has_perm("mwmbl.change_domain_submission_status"):
        raise InvalidRequest("You do not have permission to moderate domain submissions.",
                             status=403)


@router.get(
    "/domain-submissions/queue",
    auth=JWTAuth(),
    response=ModerationQueue,
    summary="The moderation queue",
    description=(
        "Domains awaiting review, with the suggestion, the sample pages and the vote counts "
        "for each one. Requires the `change_domain_submission_status` permission.\n\n"
        "One row per *domain*, not per submission: a domain submitted nine times is one card "
        "carrying `submission_count` of 9, and one decision settles all nine. `count` is "
        "therefore distinct pending domains.\n\n"
        "Suggestions are precomputed when a domain is submitted, so this endpoint runs no "
        "model: filtering, ordering and pagination all happen in the database. A domain "
        "still being crawled comes back with `evidence_state` of `PENDING` and no suggestion, "
        "rather than a placeholder.\n\n"
        "`order_by=submissions` (the default) is the order the review screen shows - most "
        "asked for first, then most upvoted. `needs_review` puts confident rejections first, "
        "then rows the tool is unsure about, then confident approvals, and finally domains "
        "that have not been crawled yet. `oldest` and `confidence` are also accepted."
    ),
)
def get_moderation_queue(
        request,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "submissions",
        suggested_action: str = None,
        suggested_reason: str = None,
        submitted_by: int = None,
        min_confidence: float = None,
) -> dict:
    check_moderator(request)

    # Filtering and ordering happen on the suggestion as displayed, not as stored: the two
    # differ wherever a live check adjusts the cached row, and a filter that disagrees with
    # the screen is worse than no filter. annotate_queue keeps all of it in SQL - building the
    # list in Python would mean materialising every pending submission (there are ~4,000) on
    # every request, which is the whole reason suggestions are precomputed. one_row_per_domain
    # and annotate_votes are in SQL for the same reason: the ordering sorts on both.
    submissions = annotate_votes(one_row_per_domain(
        annotate_queue(DomainSubmission.objects.filter(status="PENDING"))
    )).select_related("submitted_by")

    if submitted_by is not None:
        submissions = submissions.filter(submitted_by_id=submitted_by)
    if suggested_action:
        submissions = submissions.filter(displayed_action=suggested_action)
    if suggested_reason:
        submissions = submissions.filter(displayed_reason=suggested_reason)
    if min_confidence is not None:
        submissions = submissions.filter(displayed_confidence__gte=min_confidence)

    submissions = submissions.order_by(*_queue_ordering(order_by))

    count = submissions.count()
    page = list(submissions[offset:offset + limit])
    return {"items": _queue_items(page), "count": count}


# Uncrawled rows sort last under every ordering: they are not more urgent than a confident
# rejection, and a moderator can do nothing with one until the crawl lands.
QUEUE_ORDERINGS = {
    "oldest": ("submitted_on",),
    "confidence": (F("displayed_confidence").desc(nulls_last=True), "submitted_on"),
    "needs_review": (F("displayed_priority").desc(nulls_last=True), "submitted_on"),
    # What the review screen says on it: "sorted by submissions, then upvotes". The number of
    # people who asked for a domain is the one signal here that is not the tool's own opinion,
    # which is why it leads rather than the suggestion's confidence.
    "submissions": (F("submission_count").desc(), F("upvotes").desc(), "submitted_on"),
}


def _queue_ordering(order_by: str):
    return QUEUE_ORDERINGS.get(order_by, QUEUE_ORDERINGS["submissions"])


def _queue_items(submissions: list[DomainSubmission]) -> list[QueueItemSchema]:
    """Build the page's rows, aggregating the live checks across the page rather than per row.

    Each ``submission`` is the earliest pending submission of its domain, carrying the
    per-domain counts one_row_per_domain and annotate_votes put on it. The pages and the
    padlock come off the DomainEvidence rows already fetched here for the suggestion, so
    showing them costs nothing.
    """
    evidence_by_domain = {row.domain: row for row in DomainEvidence.objects.filter(
        domain__in=[submission.name for submission in submissions])}
    submitters = submitter_records({submission.submitted_by_id for submission in submissions})
    priors = prior_decision_counts({submission.name for submission in submissions})

    empty = {"approved": 0, "rejected": 0}
    items = []
    for submission in submissions:
        evidence = evidence_by_domain.get(submission.name)
        suggestion = suggestion_for(
            submission, evidence,
            submitter=submitters.get(submission.submitted_by_id, empty),
            prior=priors.get(submission.name, empty))
        signals = evidence.signals if evidence else {}
        items.append(QueueItemSchema(
            name=submission.name,
            submission_count=submission.submission_count,
            first_submitted_on=submission.submitted_on,
            first_submitted_by=submission.submitted_by_id,
            first_submitted_by_username=submission.submitted_by.username,
            upvotes=submission.upvotes,
            downvotes=submission.downvotes,
            https=signals.get("https"),
            evidence_state=evidence.state if evidence else "PENDING",
            pages=evidence.pages if evidence else [],
            suggestion=suggestion.__dict__ if suggestion else None,
        ))
    return items


@router.get(
    "/domain-submissions/ids/{submission_id}",
    auth=JWTAuth(),
    response=SubmissionDetailSchema,
    summary="Everything needed to decide one submission",
    description=(
        "The submission, the pages crawled from the domain, the evidence behind the "
        "suggestion, the submitter's track record and any earlier decisions on the same "
        "domain. Requires the `change_domain_submission_status` permission."
    ),
)
def get_submission_detail(request, submission_id: int) -> SubmissionDetailSchema:
    check_moderator(request)

    submission = (DomainSubmission.objects.filter(id=submission_id)
                  .select_related("submitted_by").first())
    if submission is None:
        raise InvalidRequest("Submission not found.", status=404)

    evidence = DomainEvidence.objects.filter(domain=submission.name).first()
    suggestion = suggestion_for(submission, evidence)
    return SubmissionDetailSchema(
        id=submission.id,
        name=submission.name,
        submitted_by=submission.submitted_by_id,
        submitted_by_username=submission.submitted_by.username,
        submitted_on=submission.submitted_on,
        status=submission.status,
        rejection_reason=submission.rejection_reason,
        rejection_detail=submission.rejection_detail,
        evidence_state=evidence.state if evidence else "PENDING",
        suggestion=suggestion.__dict__ if suggestion else None,
        pages=evidence.pages if evidence else [],
        signals=evidence.signals if evidence else {},
        submitter_record=submitter_record(submission.submitted_by_id),
        prior_decisions=prior_decisions(submission),
        index_stats=_index_stats(submission.name),
    )


@router.post(
    "/domain-submissions/decisions",
    auth=JWTAuth(),
    summary="Record several moderation decisions at once",
    description=(
        "Submit a screenful of individually-made decisions in one request. Each entry carries "
        "its own status, so this cannot be used to accept every suggestion in bulk - a "
        "moderator still decides each domain.\n\n"
        "A decision is addressed to a domain and settles **every** submission of that name, "
        "including ones already decided - which is also how a past decision is changed. "
        "Requires the `change_domain_submission_status` permission."
    ),
)
def submit_decisions(request, decisions: BulkDecisionRequest):
    check_email_verified(request)
    check_moderator(request)

    names = [normalize_domain(decision.domain) for decision in decisions.decisions]
    by_name = {}
    for submission in DomainSubmission.objects.filter(name__in=names):
        by_name.setdefault(submission.name, []).append(submission)

    domains, updated, missing = 0, 0, []
    with transaction.atomic():
        for decision, name in zip(decisions.decisions, names):
            submissions = by_name.get(name)
            if not submissions:
                missing.append(decision.domain)
                continue
            # Every submission of the name, not only the pending ones. A domain is one thing
            # to a moderator, and leaving the already-decided rows behind would let a domain
            # sit half approved and half rejected - and would mean re-deciding one needed a
            # different endpoint from deciding it.
            for submission in submissions:
                apply_decision(submission, decision, request.user)
                updated += 1
            domains += 1

    return {"status": "ok", "domains": domains, "updated": updated, "not_found": missing}


@router.post(
    "/domain-submissions/domains/{domain}/undo",
    auth=JWTAuth(),
    summary="Undo the decision on a domain",
    description=(
        "Put every submission of a domain back to PENDING and clear the rejection reason and "
        "detail, so it returns to the queue. "
        "Requires the `change_domain_submission_status` permission."
    ),
)
def undo_decision(request, domain: str):
    check_email_verified(request)
    check_moderator(request)

    name = normalize_domain(domain)
    submissions = DomainSubmission.objects.filter(name=name)
    was_approved = submissions.filter(status="APPROVED").exists()
    # Deliberately leaves suggested_status, suggested_reason, suggestion_confidence and
    # suggestion_model_version alone. Those record what was on screen when the decision being
    # undone was made, which is the whole reason they are stored separately from the live
    # suggestion - and re-posting a PENDING status through apply_decision, the nearest thing
    # to an undo before this endpoint, overwrote them from the request.
    undone = submissions.update(
        status="PENDING", rejection_reason="", rejection_detail="",
        status_changed_by=request.user, status_changed_on=timezone.now())
    if not undone:
        raise InvalidRequest(f"No submissions found for {name}.", status=404)

    if was_approved:
        # .update() does not fire post_save, so the approval receiver never runs. An undone
        # approval has to reach the snapshot too: the domain was subtracted from the remote
        # blocklists when it was approved, and until the snapshot is rebuilt it stays
        # subtracted. Same debounce as mwmbl.signals.
        schedule_blacklist_rebuild()

    return {"status": "ok", "message": f"{name} is pending again.", "updated": undone}


HISTORY_ORDERINGS = {
    "recent": (F("touched").desc(), "-pk"),
    "oldest": (F("touched").asc(), "pk"),
}


@router.get(
    "/domain-submissions/moderated",
    auth=JWTAuth(),
    response=ModerationHistory,
    summary="Domains that have been moderated",
    description=(
        "Past moderations, one row per domain, newest decision first. Filter by `status` "
        "(`PENDING`, `APPROVED` or `REJECTED`; omit for any), by the `moderator` who made the "
        "call, or by exact domain `name`.\n\n"
        "Each row carries the `suggested_*` audit columns, so a moderator revisiting a "
        "decision sees what was on screen when it was made rather than what the model would "
        "say about the domain today. Changing a decision is a normal POST to "
        "`/domain-submissions/decisions`. "
        "Requires the `change_domain_submission_status` permission."
    ),
)
def get_moderation_history(
        request,
        limit: int = 50,
        offset: int = 0,
        status: str = None,
        moderator: int = None,
        name: str = None,
        order_by: str = "recent",
) -> dict:
    check_moderator(request)

    submissions = DomainSubmission.objects.all()
    if status:
        submissions = submissions.filter(status=status)
    if moderator is not None:
        submissions = submissions.filter(status_changed_by_id=moderator)
    if name:
        submissions = submissions.filter(name=normalize_domain(name))

    # "last" rather than "first": a history row shows the decision that currently stands, so
    # the domain is represented by its most recently touched submission, not its oldest.
    submissions = one_row_per_domain(submissions, status=status, pick="last")
    submissions = (submissions.select_related("submitted_by", "status_changed_by")
                   .order_by(*HISTORY_ORDERINGS.get(order_by, HISTORY_ORDERINGS["recent"])))

    count = submissions.count()
    return {"items": _moderated_items(list(submissions[offset:offset + limit])), "count": count}


def _moderated_items(submissions: list[DomainSubmission]) -> list[ModeratedDomainSchema]:
    """Build the page's rows. The representative row here is the last-touched submission, so
    "who first asked for this domain" needs one more lookup - once for the page, not per row."""
    first_by_name = {}
    for submission in (DomainSubmission.objects
                       .filter(name__in=[row.name for row in submissions])
                       .select_related("submitted_by").order_by("submitted_on", "pk")):
        first_by_name.setdefault(submission.name, submission)

    items = []
    for submission in submissions:
        first = first_by_name[submission.name]
        items.append(ModeratedDomainSchema(
            name=submission.name,
            submission_count=submission.submission_count,
            status=submission.status,
            rejection_reason=submission.rejection_reason,
            rejection_detail=submission.rejection_detail,
            first_submitted_on=first.submitted_on,
            first_submitted_by=first.submitted_by_id,
            first_submitted_by_username=first.submitted_by.username,
            status_changed_on=submission.status_changed_on,
            status_changed_by=submission.status_changed_by_id,
            status_changed_by_username=(submission.status_changed_by.username
                                        if submission.status_changed_by else ""),
            suggested_status=submission.suggested_status,
            suggested_reason=submission.suggested_reason,
            suggestion_confidence=submission.suggestion_confidence,
            suggestion_model_version=submission.suggestion_model_version,
        ))
    return items


@router.post(
    "/domain-submissions/ids/{submission_id}/refetch",
    auth=JWTAuth(),
    summary="Re-crawl a submitted domain",
    description=(
        "Queue a fresh crawl of the domain and recompute its suggestion - for when a site was "
        "simply down when it was first checked. "
        "Requires the `change_domain_submission_status` permission."
    ),
)
def refetch_submission_evidence(request, submission_id: int):
    check_moderator(request)

    submission = DomainSubmission.objects.filter(id=submission_id).first()
    if submission is None:
        raise InvalidRequest("Submission not found.", status=404)

    # Clearing the row is what makes the crawl actually happen: the task skips domains whose
    # evidence is still fresh, which is the behaviour a re-fetch is asking to override.
    DomainEvidence.objects.filter(domain=submission.name).delete()
    enrich_domain_submission(submission.name)
    return {"status": "ok", "message": f"Queued a re-crawl of {submission.name}."}


def _index_stats(domain: str) -> dict:
    """What the crawler already knows about this domain, when Redis is reachable."""
    try:
        return stats_manager.get_stats_for_domain(domain).__dict__
    except Exception:
        logger.warning("Could not read index stats for %s", domain, exc_info=True)
        return {}


@router.post(
    "/search-results/vote",
    auth=JWTAuth(),
    summary="Vote on a search result",
    description=(
        "Cast an upvote or downvote on a search result for a specific query. "
        "If the user has already voted on this result for this query, the vote will be updated. "
        "Each user can only have one vote per URL per query. "
        "Requires a verified account."
    ),
    tags=["Search Result Voting"],
)
def vote_on_search_result(request, vote_request: VoteRequest):
    check_email_verified(request)

    if vote_request.vote_type not in SearchResultVote.VOTE_TYPES:
        raise InvalidRequest("Invalid vote type. Must be 'upvote' or 'downvote'.", status=400)

    vote, created = SearchResultVote.objects.update_or_create(
        user=request.user,
        url=vote_request.url,
        query=vote_request.query,
        defaults={'vote_type': vote_request.vote_type}
    )

    action = "created" if created else "updated"
    return {"status": "ok", "message": f"Vote {action} successfully."}


@router.post(
    "/search-results/votes",
    response=VoteResponse,
    auth=JWTAuth(),
    summary="Get vote statistics for search results",
    description=(
        "Retrieve vote counts (upvotes and downvotes) for multiple URLs in the context of a "
        "specific search query. Also returns the current user's vote on each result if they have "
        "voted. This endpoint uses POST to handle large numbers of URLs that would exceed URL "
        "length limits. Requires a verified account."
    ),
    tags=["Search Result Voting"],
)
def get_vote_counts(request, vote_stats_request: VoteStatsRequest):
    check_email_verified(request)

    if not vote_stats_request.urls:
        raise InvalidRequest("At least one URL must be provided.", status=400)

    vote_data = {}
    for url in vote_stats_request.urls:
        votes = SearchResultVote.objects.filter(url=url, query=vote_stats_request.query)
        upvotes = votes.filter(vote_type='upvote').count()
        downvotes = votes.filter(vote_type='downvote').count()

        user_vote = None
        try:
            user_vote_obj = votes.get(user=request.user)
            user_vote = user_vote_obj.vote_type
        except SearchResultVote.DoesNotExist:
            pass

        vote_data[url] = VoteStats(
            upvotes=upvotes,
            downvotes=downvotes,
            user_vote=user_vote
        )

    return VoteResponse(votes=vote_data)


@router.delete(
    "/search-results/vote",
    auth=JWTAuth(),
    summary="Remove a vote from a search result",
    description=(
        "Remove the current user's vote (upvote or downvote) from a specific search result for a "
        "given query. If the user has not voted on this result for this query, a 404 error will "
        "be returned. Requires a verified account."
    ),
    tags=["Search Result Voting"],
)
def remove_vote(request, vote_request: VoteRemoveRequest):
    check_email_verified(request)

    try:
        vote = SearchResultVote.objects.get(
            user=request.user,
            url=vote_request.url,
            query=vote_request.query
        )
        vote.delete()
        return {"status": "ok", "message": "Vote removed successfully."}
    except SearchResultVote.DoesNotExist:
        raise InvalidRequest("No vote found to remove.", status=404)


@router.get(
    "/search-results/my-votes",
    response=list[UserVoteHistory],
    auth=JWTAuth(),
    summary="Get user's voting history",
    description=(
        "Retrieve the current user's complete voting history, showing all votes they have cast "
        "on search results. Results are ordered by timestamp (most recent first) and paginated. "
        "Requires a verified account."
    ),
    tags=["Search Result Voting"],
)
@paginate
def get_user_vote_history(request) -> list[SearchResultVote]:
    check_email_verified(request)
    return SearchResultVote.objects.filter(user=request.user).order_by('-timestamp')


# ---------------------------------------------------------------------------
# Agreements helpers
# ---------------------------------------------------------------------------

def _record_agreements(user: MwmblUser, agreement_types: list) -> None:
    for agreement_type in agreement_types:
        version_id = settings.CURRENT_AGREEMENT_VERSIONS.get(agreement_type)
        if version_id:
            UserAgreement.objects.create(
                user=user,
                agreement_type=agreement_type,
                version_id=version_id,
            )


def _record_marketing_consent(user: MwmblUser, source: MarketingSource, opted_in: bool) -> MarketingConsent:
    """
    Append a consent decision for this (user, source), but only when it changes the
    current state. Repeated identical decisions — e.g. mail-client scanners POSTing
    the one-click unsubscribe URL, or a user re-confirming an existing choice — return
    the existing row instead of growing the audit trail. Returns the row representing
    the current state.
    """
    latest = (
        MarketingConsent.objects.filter(user=user, source=source)
        .order_by("-timestamp", "-id")
        .first()
    )
    if latest is not None and latest.opted_in == opted_in:
        return latest
    return MarketingConsent.objects.create(user=user, source=source, opted_in=opted_in)


_UNSUBSCRIBE_SALT = "marketing-unsubscribe"


def make_unsubscribe_token(user: MwmblUser, source: MarketingSource) -> str:
    """
    Build a signed, URL-safe token identifying a (user, source) pair for one-click
    unsubscribe. No expiry: unsubscribe links in old emails must keep working.
    """
    return signing.dumps({"user_id": user.id, "source": source}, salt=_UNSUBSCRIBE_SALT)


def _require_current_agreement(user: MwmblUser, agreement_type: AgreementType) -> None:
    current_version = settings.CURRENT_AGREEMENT_VERSIONS.get(agreement_type)
    accepted = UserAgreement.objects.filter(
        user=user,
        agreement_type=agreement_type,
        version_id=current_version,
    ).exists()
    if not accepted:
        raise InvalidRequest(
            f"You must accept the current {agreement_type} (version {current_version}) before using this feature.",
            status=403,
        )


# ---------------------------------------------------------------------------
# Agreement endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/agreements/",
    auth=JWTAuth(),
    response=AgreementResponse,
    summary="Accept a terms agreement",
    description=(
        "Record acceptance of a terms agreement for the authenticated user. "
        "The server stamps the current version and timestamp — the client only supplies the agreement type. "
        "Calling this again after a version update creates a new acceptance record for the new version. "
        "Requires a verified account."
    ),
    tags=["Agreements"],
)
def accept_agreement(request, body: AgreementAcceptRequest):
    check_email_verified(request)
    version_id = settings.CURRENT_AGREEMENT_VERSIONS.get(body.agreement_type)
    if not version_id:
        raise InvalidRequest("Unknown agreement type.", status=400)
    agreement = UserAgreement.objects.create(
        user=request.user,
        agreement_type=body.agreement_type,
        version_id=version_id,
    )
    return AgreementResponse(
        agreement_type=agreement.agreement_type,
        version_id=agreement.version_id,
        accepted_at=agreement.accepted_at,
    )


@router.get(
    "/agreements/",
    auth=JWTAuth(),
    response=list[AgreementResponse],
    summary="Get current agreements",
    description=(
        "Returns the most recently accepted version of each agreement type for the authenticated user. "
        "Only types the user has accepted appear in the response. "
        "Requires a verified account."
    ),
    tags=["Agreements"],
)
def get_agreements(request) -> list[AgreementResponse]:
    check_email_verified(request)
    result = []
    for agreement_type in AgreementType:
        latest = (
            UserAgreement.objects.filter(user=request.user, agreement_type=agreement_type)
            .order_by("-accepted_at")
            .first()
        )
        if latest:
            result.append(AgreementResponse(
                agreement_type=latest.agreement_type,
                version_id=latest.version_id,
                accepted_at=latest.accepted_at,
            ))
    return result


@router.get(
    "/agreements/history/",
    auth=JWTAuth(),
    response=list[AgreementResponse],
    summary="Get agreement acceptance history",
    description=(
        "Returns the full history of all agreement acceptances for the authenticated user, "
        "ordered most-recent first. Useful for compliance audits. "
        "Requires a verified account."
    ),
    tags=["Agreements"],
)
@paginate
def get_agreement_history(request) -> list[AgreementResponse]:
    check_email_verified(request)
    return UserAgreement.objects.filter(user=request.user).order_by("-accepted_at")


# ---------------------------------------------------------------------------
# Marketing consent
# ---------------------------------------------------------------------------

@router.get(
    "/marketing-consent",
    auth=JWTAuth(),
    response=MarketingConsentListResponse,
    summary="Get marketing email consent",
    description=(
        "Returns the current marketing email consent state for each source the user has a "
        "record for (`GUI` = mwmbl.org, `API` = developer.mwmbl.org). The state for a source "
        "is its most recently recorded decision. Sources with no record are omitted. "
        "Requires a verified account."
    ),
    tags=["Marketing"],
)
def get_marketing_consent(request) -> MarketingConsentListResponse:
    check_email_verified(request)
    consent = []
    for source in MarketingSource:
        latest = (
            MarketingConsent.objects.filter(user=request.user, source=source)
            .order_by("-timestamp", "-id")
            .first()
        )
        if latest:
            consent.append(MarketingConsentResponse(
                source=latest.source,
                opted_in=latest.opted_in,
                timestamp=latest.timestamp,
            ))
    return MarketingConsentListResponse(consent=consent)


@router.post(
    "/marketing-consent",
    auth=JWTAuth(),
    response=MarketingConsentResponse,
    summary="Update marketing email consent",
    description=(
        "Record a marketing email consent decision for the authenticated user against a "
        "source. Use `opted_in=false` to withdraw consent. Each call appends a new "
        "timestamped record, preserving the full consent history. Requires a verified account."
    ),
    tags=["Marketing"],
)
def update_marketing_consent(request, body: MarketingConsentRequest):
    check_email_verified(request)
    consent = _record_marketing_consent(request.user, body.source, body.opted_in)
    return MarketingConsentResponse(
        source=consent.source,
        opted_in=consent.opted_in,
        timestamp=consent.timestamp,
    )


def _unsubscribe_from_token(token: str) -> None:
    try:
        data = signing.loads(token, salt=_UNSUBSCRIBE_SALT)
    except signing.BadSignature:
        raise InvalidRequest("Invalid or malformed unsubscribe token.", status=400)
    user = MwmblUser.objects.filter(id=data["user_id"]).first()
    if user is None:
        raise InvalidRequest("Unknown user.", status=400)
    _record_marketing_consent(user, data["source"], opted_in=False)


@router.post(
    "/marketing/unsubscribe",
    auth=None,
    summary="One-click unsubscribe from marketing emails",
    description=(
        "RFC 8058 one-click unsubscribe target for the `List-Unsubscribe` email header. "
        "Takes a signed `token` identifying the recipient and source — no login required. "
        "Records an opt-out and is idempotent. Mail clients POST here with the body "
        "`List-Unsubscribe=One-Click`."
    ),
    tags=["Marketing"],
)
def unsubscribe_marketing(request, token: str):
    _unsubscribe_from_token(token)
    return {"status": "ok", "message": "You have been unsubscribed from marketing emails."}


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

@router.post(
    "/api-keys/",
    auth=JWTAuth(),
    response=ApiKeyCreatedResponse,
    summary="Create an API key",
    description=(
        "Create a new API key for the authenticated user. "
        "Use `scope='search'` (default) for the search endpoint or `scope='crawl'` for the crawler endpoint. "
        "The raw key value is returned **only once** in this response — store it securely. "
        "Requires a verified account and acceptance of the relevant terms of service."
    ),
    tags=["API Keys"],
)
def create_api_key(request, body: CreateApiKeyRequest):
    check_email_verified(request)
    if body.scope == ApiKey.Scope.CRAWL:
        _require_current_agreement(request.user, AgreementType.TERMS_OF_SERVICE_GUI)
    else:
        _require_current_agreement(request.user, AgreementType.TERMS_OF_SERVICE_API)
    from mwmbl.models import generate_api_key
    raw_key, key_hash = generate_api_key()
    api_key = ApiKey.objects.create(
        user=request.user,
        name=body.name,
        scopes=[body.scope],
        key=key_hash,
    )
    return ApiKeyCreatedResponse(
        id=api_key.id,
        key=raw_key,
        name=api_key.name,
        created_on=api_key.created_on,
        scopes=api_key.scopes,
    )


@router.get(
    "/api-keys/",
    auth=JWTAuth(),
    response=list[ApiKeyListItem],
    summary="List API keys",
    description=(
        "List all API keys belonging to the authenticated user. "
        "The raw key value is **not** included in this response. "
        "Requires a verified account."
    ),
    tags=["API Keys"],
)
def list_api_keys(request) -> list[ApiKeyListItem]:
    check_email_verified(request)
    keys = ApiKey.objects.filter(user=request.user).order_by("-created_on")
    return [
        ApiKeyListItem(
            id=k.id,
            name=k.name,
            created_on=k.created_on,
            scopes=k.scopes,
        )
        for k in keys
    ]


@router.delete(
    "/api-keys/{key_id}",
    auth=JWTAuth(),
    summary="Revoke an API key",
    description=(
        "Permanently revoke (delete) an API key owned by the authenticated user. "
        "Any subsequent requests using the revoked key will receive a 401 response. "
        "Returns 404 if the key does not exist or does not belong to the current user. "
        "Requires a verified account."
    ),
    tags=["API Keys"],
)
def delete_api_key(request, key_id: int):
    check_email_verified(request)
    api_key = ApiKey.objects.filter(id=key_id, user=request.user).first()
    if api_key is None:
        raise InvalidRequest("API key not found.", status=404)
    invalidate_api_key_cache(api_key.key)
    api_key.delete()
    return {"status": "ok", "message": "API key revoked."}


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

@router.get(
    "/user",
    auth=JWTAuth(),
    response=UserProfileResponse,
    summary="Get current user profile",
    description="Returns the authenticated user's username, email, plan, and email confirmation status.",
    tags=["Users"],
)
def get_current_user(request):
    check_email_verified(request)
    user = request.user
    billing = getattr(user, "billing", None)
    spend_cents = billing.max_monthly_spend_cents if billing else 0
    email_address = user.emailaddress_set.first()
    return UserProfileResponse(
        username=user.username,
        email=user.email,
        plan="free" if spend_cents == 0 else "pay-as-you-go",
        email_confirmed=email_address.verified if email_address else False,
    )


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

def _subscription_response(user, billing, status: str) -> SubscriptionResponse:
    from mwmbl.quota import get_monthly_count
    spend_cents = billing.max_monthly_spend_cents if billing else 0
    usage = get_monthly_count(user.id)
    return SubscriptionResponse(
        status=status,
        max_monthly_spend_cents=spend_cents,
        monthly_limit=pricing.effective_monthly_request_cap(spend_cents),
        monthly_usage=usage,
        estimated_cost_cents=pricing.estimated_cost_cents(usage),
        current_period_end=billing.current_period_end if billing else None,
        polar_customer_id=billing.polar_customer_id if billing else None,
    )


@router.get(
    "/billing/subscription",
    auth=JWTAuth(),
    response=SubscriptionResponse,
    summary="Get current subscription",
    description="Returns the user's spend limit, quota, and current-period usage.",
    tags=["Billing"],
)
def get_subscription(request):
    check_email_verified(request)
    user = request.user
    billing = getattr(user, "billing", None)
    spend_cents = billing.max_monthly_spend_cents if billing else 0
    if spend_cents == 0:
        status = "free"
    elif billing and billing.cancel_at_period_end:
        status = "canceling"
    else:
        status = "active"
    return _subscription_response(user, billing, status)


@router.post(
    "/billing/checkout",
    auth=JWTAuth(),
    response=CheckoutResponse,
    summary="Create Polar checkout session",
    description="Creates a Polar hosted-checkout session and returns a redirect URL.",
    tags=["Billing"],
)
def create_checkout(request, body: CheckoutRequest):
    check_email_verified(request)
    product_id = settings.POLAR_PRODUCT_ID_USAGE
    if not product_id:
        raise InvalidRequest("Billing is not configured. Contact support.", status=503)
    checkout_params = {
        "products": [product_id],
        "external_customer_id": str(request.user.id),
        "metadata": {"user_id": str(request.user.id)},
    }
    if body.success_url:
        checkout_params["success_url"] = body.success_url
    if body.embed_origin:
        checkout_params["embed_origin"] = body.embed_origin
    with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
        result = polar.checkouts.create(request=checkout_params)
    return CheckoutResponse(checkout_url=result.url)


@router.post(
    "/billing/uncancel",
    auth=JWTAuth(),
    response=SubscriptionResponse,
    summary="Uncancel a pending subscription cancellation",
    description="Removes a scheduled cancellation, keeping the subscription active beyond the current period end.",
    tags=["Billing"],
)
def uncancel_subscription(request):
    check_email_verified(request)
    billing = getattr(request.user, "billing", None)
    if not billing or not billing.polar_subscription_id:
        raise InvalidRequest("No active subscription found.", status=404)
    if not billing.cancel_at_period_end:
        raise InvalidRequest("Subscription is not scheduled to cancel.", status=409)
    with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
        result = polar.subscriptions.update(
            id=billing.polar_subscription_id,
            subscription_update=SubscriptionCancel(cancel_at_period_end=False),
        )
    billing.current_period_end = result.current_period_end
    billing.cancel_at_period_end = False
    billing.save()
    return _subscription_response(request.user, billing, "active")


@router.post(
    "/billing/cancel",
    auth=JWTAuth(),
    response=SubscriptionResponse,
    summary="Cancel subscription at period end",
    description="Schedules the subscription to cancel at the end of the current billing period. "
                "The spend limit remains active until then.",
    tags=["Billing"],
)
def cancel_subscription(request):
    check_email_verified(request)
    billing = getattr(request.user, "billing", None)
    if not billing or not billing.polar_subscription_id:
        raise InvalidRequest("No active subscription found.", status=404)
    try:
        with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
            result = polar.subscriptions.update(
                id=billing.polar_subscription_id,
                subscription_update=SubscriptionCancel(cancel_at_period_end=True),
            )
    except polar_models.AlreadyCanceledSubscription:
        raise InvalidRequest("Subscription is already canceled.", status=409)
    billing.current_period_end = result.current_period_end
    billing.cancel_at_period_end = True
    billing.save()
    return _subscription_response(request.user, billing, "canceling")


@router.post(
    "/billing/spend-limit",
    auth=JWTAuth(),
    response=SubscriptionResponse,
    summary="Set monthly spend limit",
    description=(
        "Sets the maximum amount (in cents) the account may be billed per month "
        "for metered API usage beyond the free 2,000 requests. Set to 0 to stay "
        "free-tier-only (hard-capped at 2,000 requests/month). Requires an active "
        "Polar subscription if raising above 0 for the first time — "
        "call POST /billing/checkout first if no subscription exists."
    ),
    tags=["Billing"],
)
def update_spend_limit(request, body: UpdateSpendLimitRequest):
    check_email_verified(request)
    billing = getattr(request.user, "billing", None)
    if body.max_monthly_spend_cents > 0 and (not billing or not billing.polar_subscription_id):
        raise InvalidRequest(
            "An active billing subscription is required before raising your spend limit "
            "above $0. Call POST /billing/checkout first.",
            status=409,
        )
    if billing is None:
        billing = UserBilling.objects.create(user=request.user)
    billing.max_monthly_spend_cents = body.max_monthly_spend_cents
    billing.save()
    if billing.max_monthly_spend_cents == 0:
        status = "free"
    elif billing.cancel_at_period_end:
        status = "canceling"
    else:
        status = "active"
    return _subscription_response(request.user, billing, status)


@router.post(
    "/billing/webhook",
    summary="Polar webhook receiver",
    description="Receives signed webhook events from Polar and keeps the user's billing state in sync.",
    tags=["Billing"],
)
def polar_webhook(request):
    logger.info("Polar webhook received")
    try:
        event = validate_event(
            body=request.body,
            headers=dict(request.headers),
            secret=settings.POLAR_WEBHOOK_SECRET,
        )
    except WebhookVerificationError:
        logger.warning("Polar webhook: invalid signature")
        raise InvalidRequest("Invalid signature", status=400)

    event_type = event.TYPE
    logger.info("Polar webhook event type=%s", event_type)

    if event_type in ("subscription.active", "subscription.updated", "subscription.uncanceled"):
        user_id = event.data.metadata.get("user_id")
        logger.info("Polar webhook: %s user_id=%s", event_type, user_id)
        user = MwmblUser.objects.filter(id=user_id).first()
        if user is None:
            logger.warning("Polar webhook: no user found for user_id=%s", user_id)
        else:
            billing, created = UserBilling.objects.get_or_create(user=user)
            logger.info("Polar webhook: UserBilling %s for user %s customer_id=%s subscription_id=%s", "created" if created else "updated", user.email, event.data.customer_id, event.data.id)
            billing.polar_customer_id = event.data.customer_id or billing.polar_customer_id
            billing.polar_subscription_id = event.data.id or billing.polar_subscription_id
            billing.current_period_end = event.data.current_period_end or billing.current_period_end
            billing.cancel_at_period_end = False
            billing.save()
    elif event_type == "subscription.canceled":
        user_id = event.data.metadata.get("user_id")
        logger.info("Polar webhook: subscription.canceled user_id=%s cancel_at_period_end=%s", user_id, getattr(event.data, "cancel_at_period_end", None))
        user = MwmblUser.objects.filter(id=user_id).first()
        if user is None:
            logger.warning("Polar webhook: no user found for user_id=%s", user_id)
        elif getattr(event.data, "cancel_at_period_end", False):
            # Cancellation is scheduled; subscription still active until period end.
            billing = getattr(user, "billing", None)
            if billing:
                billing.cancel_at_period_end = True
                billing.save()
            logger.info("Polar webhook: user %s (id=%s) subscription scheduled to cancel at period end", user.email, user_id)
        else:
            logger.info("Polar webhook: immediate cancellation for user %s (id=%s), resetting spend limit to $0", user.email, user_id)
            billing = getattr(user, "billing", None)
            if billing:
                billing.cancel_at_period_end = False
                billing.max_monthly_spend_cents = 0
                billing.save()
    elif event_type == "subscription.revoked":
        user_id = event.data.metadata.get("user_id")
        logger.info("Polar webhook: subscription.revoked user_id=%s", user_id)
        user = MwmblUser.objects.filter(id=user_id).first()
        if user is None:
            logger.warning("Polar webhook: no user found for user_id=%s", user_id)
        else:
            logger.info("Polar webhook: resetting user %s (id=%s) spend limit to $0", user.email, user_id)
            billing = getattr(user, "billing", None)
            if billing:
                billing.cancel_at_period_end = False
                billing.max_monthly_spend_cents = 0
                billing.save()
    else:
        logger.info("Polar webhook: unhandled event type=%s, ignoring", event_type)

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

@router.post(
    "/forgot-password",
    summary="Request password reset",
    description="Sends a password-reset email. Always returns 200 to prevent user enumeration.",
    tags=["Auth"],
)
def forgot_password(request, body: ForgotPasswordRequest):
    from django.contrib.auth.forms import PasswordResetForm
    form = PasswordResetForm({"email": body.email})
    if form.is_valid():
        form.save(request=request, use_https=request.is_secure())
    return {}


@router.post(
    "/reset-password",
    summary="Confirm password reset",
    description="Validates the reset token and sets a new password.",
    tags=["Auth"],
)
def reset_password(request, body: ResetPasswordRequest):
    from django.contrib.auth.tokens import default_token_generator
    try:
        user = MwmblUser.objects.get(email=body.email)
    except MwmblUser.DoesNotExist:
        raise InvalidRequest("Invalid or expired reset token.", status=400)
    if not default_token_generator.check_token(user, body.key):
        raise InvalidRequest("Invalid or expired reset token.", status=400)
    user.set_password(body.new_password)
    user.save()
    return {}
