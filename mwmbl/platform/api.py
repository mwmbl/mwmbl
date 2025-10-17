from allauth.account.adapter import get_adapter
from allauth.account.models import EmailConfirmationHMAC
from allauth.account.utils import setup_user_email, send_email_confirmation
from django.db.models import Count, Q
from ninja.pagination import paginate
from ninja_extra import NinjaExtraAPI
from ninja_jwt.authentication import JWTAuth
from ninja_jwt.controller import NinjaJWTDefaultController

from mwmbl.models import MwmblUser, DomainSubmission, SearchResultVote
from mwmbl.platform.schemas import (
    Registration, ConfirmEmail, DomainSubmissionSchema, UpdateDomainSubmission,
    VoteRequest, VoteRemoveRequest, VoteStatsRequest, VoteResponse, VoteStats, UserVoteHistory
)

api = NinjaExtraAPI(urls_namespace="platform")
api.register_controllers(NinjaJWTDefaultController)


class InvalidRequest(Exception):
    def __init__(self, message: str, status: int = 403):
        self.message = message
        self.status = status


@api.exception_handler(InvalidRequest)
def no_permission(request, exc: InvalidRequest):
    return api.create_response(
        request,
        {"status": "error", "message": exc.message},
        status=exc.status,
    )


def check_email_verified(request):
    from_email_address = request.user.emailaddress_set.first()
    if not from_email_address.verified:
        raise InvalidRequest("Email address is not verified")


@api.post('/register')
def register(request, registration: Registration):
    # Check for existing user with this username
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


@api.post("/confirm-email")
def confirm_email(request, confirm: ConfirmEmail):
    confirmation = EmailConfirmationHMAC.from_key(confirm.key)
    if confirmation is None:
        raise InvalidRequest("Invalid confirmation key")

    # Check the signed email address matches this one
    if confirmation.email_address.email != confirm.email:
        raise InvalidRequest("Invalid username or email")

    # Check the username matches
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


@api.get("/protected", auth=JWTAuth())
def protected(request):
    check_email_verified(request)
    return {"status": "ok", "message": "You are authenticated!"}


@api.delete("/users/{username}", auth=JWTAuth())
def delete_user(request, username: str):
    user = MwmblUser.objects.get(username=username)
    if user is None:
        raise InvalidRequest("User not found.", status=404)

    if user != request.user:
        raise InvalidRequest("You can only delete your own account.")

    user.delete()
    return {"status": "ok", "message": "User deleted."}


@api.get("/domain-submissions/domains/{domain}", response=list[DomainSubmissionSchema])
@paginate
def get_domain_submissions_for_domain(request, domain: str) -> list[DomainSubmissionSchema]:
    return DomainSubmission.objects.filter(name=domain).all()


@api.get("/domain-submissions", response=list[DomainSubmissionSchema])
@paginate
def get_domain_submissions(request) -> list[DomainSubmission]:
    return DomainSubmission.objects.all()


@api.post("/domain-submissions/", auth=JWTAuth())
def submit_domain(request, domain: str):
    check_email_verified(request)
    submission = DomainSubmission(name=domain, submitted_by=request.user)
    submission.save()
    return {"status": "ok", "message": "Domain submitted for review."}


@api.delete("/domain-submissions/ids/{submission_id}", auth=JWTAuth())
def delete_submission(request, submission_id: int):
    check_email_verified(request)
    submission = DomainSubmission.objects.get(id=submission_id)
    if submission is None:
        raise InvalidRequest("Submission not found.", status=404)

    if request.user != submission.submitted_by:
        raise InvalidRequest("You can only delete your own submissions.")

    submission.delete()
    return {"status": "ok", "message": "Submission deleted."}


@api.post("/domain-submissions/ids/{submission_id}", auth=JWTAuth())
def update_submission_status(request, submission_id: int, update_submission: UpdateDomainSubmission):
    check_email_verified(request)
    submission = DomainSubmission.objects.get(id=submission_id)
    if submission is None:
        raise InvalidRequest("Submission not found.", status=404)

    # Can only update if the user has the permission
    if not request.user.has_perm("change_domain_submission_status"):
        raise InvalidRequest("You do not have permission to update this submission.")

    submission.status = update_submission.status
    submission.rejection_reason = update_submission.rejection_reason
    submission.rejection_detail = update_submission.rejection_detail
    submission.save()
    return {"status": "ok", "message": "Submission updated."}


@api.post(
    "/search-results/vote", 
    auth=JWTAuth(),
    summary="Vote on a search result",
    description="Cast an upvote or downvote on a search result for a specific query. "
                "If the user has already voted on this result for this query, the vote will be updated. "
                "Each user can only have one vote per URL per query.",
    tags=["Search Result Voting"]
)
def vote_on_search_result(request, vote_request: VoteRequest):
    check_email_verified(request)
    
    # Validate vote type
    if vote_request.vote_type not in SearchResultVote.VOTE_TYPES:
        raise InvalidRequest("Invalid vote type. Must be 'upvote' or 'downvote'.", status=400)
    
    # Create or update the vote
    vote, created = SearchResultVote.objects.update_or_create(
        user=request.user,
        url=vote_request.url,
        query=vote_request.query,
        defaults={'vote_type': vote_request.vote_type}
    )
    
    action = "created" if created else "updated"
    return {"status": "ok", "message": f"Vote {action} successfully."}


@api.post(
    "/search-results/votes", 
    response=VoteResponse, 
    auth=JWTAuth(),
    summary="Get vote statistics for search results",
    description="Retrieve vote counts (upvotes and downvotes) for multiple URLs in the context of a specific search query. "
                "Also returns the current user's vote on each result if they have voted. "
                "This endpoint uses POST to handle large numbers of URLs that would exceed URL length limits.",
    tags=["Search Result Voting"]
)
def get_vote_counts(request, vote_stats_request: VoteStatsRequest):
    check_email_verified(request)
    
    if not vote_stats_request.urls:
        raise InvalidRequest("At least one URL must be provided.", status=400)
    
    # Get vote counts for each URL
    vote_data = {}
    for url in vote_stats_request.urls:
        # Get aggregated vote counts
        votes = SearchResultVote.objects.filter(url=url, query=vote_stats_request.query)
        upvotes = votes.filter(vote_type='upvote').count()
        downvotes = votes.filter(vote_type='downvote').count()
        
        # Get user's vote if any
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


@api.delete(
    "/search-results/vote", 
    auth=JWTAuth(),
    summary="Remove a vote from a search result",
    description="Remove the current user's vote (upvote or downvote) from a specific search result for a given query. "
                "If the user has not voted on this result for this query, a 404 error will be returned.",
    tags=["Search Result Voting"]
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


@api.get(
    "/search-results/my-votes", 
    response=list[UserVoteHistory], 
    auth=JWTAuth(),
    summary="Get user's voting history",
    description="Retrieve the current user's complete voting history, showing all votes they have cast on search results. "
                "Results are ordered by timestamp (most recent first) and paginated.",
    tags=["Search Result Voting"]
)
@paginate
def get_user_vote_history(request) -> list[SearchResultVote]:
    check_email_verified(request)
    return SearchResultVote.objects.filter(user=request.user).order_by('-timestamp')
