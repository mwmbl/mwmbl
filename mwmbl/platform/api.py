import logging

from allauth.account.adapter import get_adapter
from allauth.account.models import EmailConfirmationHMAC
from allauth.account.utils import setup_user_email, send_email_confirmation
from django.conf import settings
from django.utils import timezone
from ninja import Router
from ninja.pagination import paginate
from ninja_jwt.authentication import JWTAuth
from polar_sdk import Polar
from polar_sdk import models as polar_models
from polar_sdk.models import SubscriptionCancel, CustomerSubscriptionUpdateProduct
from polar_sdk.webhooks import validate_event, WebhookVerificationError

from mwmbl.exceptions import InvalidRequest
from mwmbl.search_auth import invalidate_api_key_cache, invalidate_user_api_key_cache
from mwmbl.models import AgreementType, MwmblUser, DomainSubmission, SearchResultVote, ApiKey, UsageBucket, UserBilling, UserAgreement, generate_username
from mwmbl.platform.schemas import (
    Registration, ConfirmEmail, DomainSubmissionSchema, UpdateDomainSubmission,
    VoteRequest, VoteRemoveRequest, VoteStatsRequest, VoteResponse, VoteStats, UserVoteHistory,
    CreateApiKeyRequest, ApiKeyCreatedResponse, ApiKeyListItem,
    UserProfileResponse, SubscriptionResponse, CheckoutRequest, CheckoutResponse, ChangePlanRequest,
    ForgotPasswordRequest, ResetPasswordRequest,
    AgreementAcceptRequest, AgreementResponse,
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
    description="Returns the authenticated user's username, email, plan tier, and email confirmation status.",
    tags=["Users"],
)
def get_current_user(request):
    check_email_verified(request)
    user = request.user
    email_address = user.emailaddress_set.first()
    return UserProfileResponse(
        username=user.username,
        email=user.email,
        plan=user.tier,
        email_confirmed=email_address.verified if email_address else False,
    )


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

@router.get(
    "/billing/subscription",
    auth=JWTAuth(),
    response=SubscriptionResponse,
    summary="Get current subscription",
    description="Returns the user's active plan, quota, and current-period usage.",
    tags=["Billing"],
)
def get_subscription(request):
    from mwmbl.quota import get_monthly_count
    check_email_verified(request)
    user = request.user
    billing = getattr(user, "billing", None)
    usage = get_monthly_count(user.id)
    if user.tier == MwmblUser.Tier.FREE:
        status = "free"
    elif billing and billing.cancel_at_period_end:
        status = "canceling"
    else:
        status = "active"
    return SubscriptionResponse(
        plan=user.tier,
        status=status,
        monthly_limit=MwmblUser.TIER_MONTHLY_LIMITS[user.tier],
        monthly_usage=usage,
        current_period_end=billing.current_period_end if billing else None,
        polar_customer_id=billing.polar_customer_id if billing else None,
    )


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
    product_map = {
        "starter": settings.POLAR_PRODUCT_ID_STARTER,
        "pro": settings.POLAR_PRODUCT_ID_PRO,
    }
    product_id = product_map[body.plan]
    if not product_id:
        raise InvalidRequest(f"Plan '{body.plan}' is not configured. Contact support.", status=503)
    billing = getattr(request.user, "billing", None)
    checkout_params = {
        "products": [product_id],
        "metadata": {"user_id": str(request.user.id)},
    }
    if billing and billing.polar_customer_id:
        checkout_params["external_customer_id"] = billing.polar_customer_id
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
    from mwmbl.quota import get_monthly_count
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
    usage = get_monthly_count(request.user.id)
    return SubscriptionResponse(
        plan=request.user.tier,
        status="active",
        monthly_limit=MwmblUser.TIER_MONTHLY_LIMITS[request.user.tier],
        monthly_usage=usage,
        current_period_end=billing.current_period_end,
        polar_customer_id=billing.polar_customer_id,
    )


@router.post(
    "/billing/cancel",
    auth=JWTAuth(),
    response=SubscriptionResponse,
    summary="Cancel subscription at period end",
    description="Schedules the subscription to cancel at the end of the current billing period. "
                "The plan remains active until then.",
    tags=["Billing"],
)
def cancel_subscription(request):
    from mwmbl.quota import get_monthly_count
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
    usage = get_monthly_count(request.user.id)
    return SubscriptionResponse(
        plan=request.user.tier,
        status="canceling",
        monthly_limit=MwmblUser.TIER_MONTHLY_LIMITS[request.user.tier],
        monthly_usage=usage,
        current_period_end=billing.current_period_end,
        polar_customer_id=billing.polar_customer_id,
    )


@router.post(
    "/billing/change-plan",
    auth=JWTAuth(),
    response=SubscriptionResponse,
    summary="Change subscription plan",
    description="Upgrades or downgrades to a different paid plan. Takes effect immediately "
                "with proration applied to the next invoice. The tier shown in the response "
                "reflects the current state; the new tier arrives via webhook.",
    tags=["Billing"],
)
def change_plan(request, body: ChangePlanRequest):
    from mwmbl.quota import get_monthly_count
    check_email_verified(request)
    billing = getattr(request.user, "billing", None)
    if not billing or not billing.polar_subscription_id:
        raise InvalidRequest("No active subscription found.", status=404)
    product_map = {
        "starter": settings.POLAR_PRODUCT_ID_STARTER,
        "pro": settings.POLAR_PRODUCT_ID_PRO,
    }
    product_id = product_map[body.plan]
    if not product_id:
        raise InvalidRequest(f"Plan '{body.plan}' is not configured. Contact support.", status=503)
    with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
        result = polar.subscriptions.update(
            id=billing.polar_subscription_id,
            subscription_update=CustomerSubscriptionUpdateProduct(product_id=product_id),
        )
    billing.current_period_end = result.current_period_end
    billing.save()
    usage = get_monthly_count(request.user.id)
    if request.user.tier == MwmblUser.Tier.FREE:
        status = "free"
    elif billing.cancel_at_period_end:
        status = "canceling"
    else:
        status = "active"
    return SubscriptionResponse(
        plan=request.user.tier,
        status=status,
        monthly_limit=MwmblUser.TIER_MONTHLY_LIMITS[request.user.tier],
        monthly_usage=usage,
        current_period_end=billing.current_period_end,
        polar_customer_id=billing.polar_customer_id,
    )


@router.post(
    "/billing/webhook",
    summary="Polar webhook receiver",
    description="Receives signed webhook events from Polar and updates the user's plan.",
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
    logger.info("Polar webhook event type=%s product_id=%s", event_type, getattr(event.data, "product_id", None))

    product_tier = {
        settings.POLAR_PRODUCT_ID_STARTER: MwmblUser.Tier.STARTER,
        settings.POLAR_PRODUCT_ID_PRO: MwmblUser.Tier.PRO,
    }

    if event_type in ("subscription.active", "subscription.updated", "subscription.uncanceled"):
        user_id = event.data.metadata.get("user_id")
        logger.info("Polar webhook: %s user_id=%s", event_type, user_id)
        user = MwmblUser.objects.filter(id=user_id).first()
        if user is None:
            logger.warning("Polar webhook: no user found for user_id=%s", user_id)
        else:
            tier = product_tier.get(event.data.product_id, MwmblUser.Tier.FREE)
            logger.info("Polar webhook: setting user %s (id=%s) tier to %s (product_id=%s)", user.email, user_id, tier, event.data.product_id)
            if tier == MwmblUser.Tier.FREE:
                logger.warning("Polar webhook: product_id=%s not found in product_tier map, defaulting to FREE", event.data.product_id)
            user.tier = tier
            user.save()
            invalidate_user_api_key_cache(user.id)
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
            logger.info("Polar webhook: immediate cancellation for user %s (id=%s), reverting to FREE", user.email, user_id)
            user.tier = MwmblUser.Tier.FREE
            user.save()
            invalidate_user_api_key_cache(user.id)
            billing = getattr(user, "billing", None)
            if billing:
                billing.cancel_at_period_end = False
                billing.save()
    elif event_type == "subscription.revoked":
        user_id = event.data.metadata.get("user_id")
        logger.info("Polar webhook: subscription.revoked user_id=%s", user_id)
        user = MwmblUser.objects.filter(id=user_id).first()
        if user is None:
            logger.warning("Polar webhook: no user found for user_id=%s", user_id)
        else:
            logger.info("Polar webhook: reverting user %s (id=%s) to FREE", user.email, user_id)
            user.tier = MwmblUser.Tier.FREE
            user.save()
            invalidate_user_api_key_cache(user.id)
            billing = getattr(user, "billing", None)
            if billing:
                billing.cancel_at_period_end = False
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
