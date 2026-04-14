from allauth.account.adapter import get_adapter
from allauth.account.models import EmailConfirmationHMAC
from allauth.account.utils import setup_user_email, send_email_confirmation
from ninja import Router
from ninja.pagination import paginate
from ninja_jwt.authentication import JWTAuth

from mwmbl.exceptions import InvalidRequest
from mwmbl.models import MwmblUser, DomainSubmission, SearchResultVote
from mwmbl.platform.schemas import (
    Registration, ConfirmEmail, DomainSubmissionSchema, UpdateDomainSubmission,
    VoteRequest, VoteRemoveRequest, VoteStatsRequest, VoteResponse, VoteStats, UserVoteHistory
)

router = Router(tags=["Platform"])


def check_email_verified(request):
    from_email_address = request.user.emailaddress_set.first()
    if not from_email_address.verified:
        raise InvalidRequest("Email address is not verified", status=403)


@router.post(
    '/register',
    summary="Register a new user",
    description=(
        "Create a new Mwmbl user account. A confirmation email will be sent to the provided "
        "address. The account cannot be used until the email is confirmed via the "
        "`/platform/confirm-email` endpoint."
    ),
)
def register(request, registration: Registration):
    if MwmblUser.objects.filter(username=registration.username).exists():
        raise InvalidRequest("Username already exists")

    user = MwmblUser(username=registration.username, email=registration.email)
    user.set_password(registration.password)
    user.save()

    setup_user_email(request, user, [])
    send_email_confirmation(request, user, signup=True)

    return {
        "status": "ok",
        "username": registration.username,
        "message": "User registered successfully. Check your email for confirmation."
    }


@router.post(
    "/confirm-email",
    summary="Confirm email address",
    description=(
        "Confirm a user's email address using the key sent in the confirmation email. "
        "The `key`, `email`, and `username` must all match the values from the confirmation email."
    ),
)
def confirm_email(request, confirm: ConfirmEmail):
    confirmation = EmailConfirmationHMAC.from_key(confirm.key)
    if confirmation is None:
        raise InvalidRequest("Invalid confirmation key")

    if confirmation.email_address.email != confirm.email:
        raise InvalidRequest("Invalid username or email")

    user = MwmblUser.objects.get(username=confirm.username)
    if user.email != confirm.email:
        raise InvalidRequest("Invalid username or email")

    adapter = get_adapter()
    adapter.confirm_email(request, confirmation.email_address)

    return {
        "status": "ok",
        "username": confirm.username,
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
    return DomainSubmission.objects.filter(name=domain).all()


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
    submission = DomainSubmission(name=domain, submitted_by=request.user)
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

    if not request.user.has_perm("change_domain_submission_status"):
        raise InvalidRequest("You do not have permission to update this submission.")

    submission.status = update_submission.status
    submission.rejection_reason = update_submission.rejection_reason
    submission.rejection_detail = update_submission.rejection_detail
    submission.save()
    return {"status": "ok", "message": "Submission updated."}


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
