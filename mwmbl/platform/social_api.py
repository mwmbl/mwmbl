"""v2 endpoints for Mwmbl Social, Mwmbl's hosted instance at social.mwmbl.org.

Registrations: a logged-in user connects a fediverse server to it, then sees and disconnects the
servers they connected. Mwmbl calls the instance's admin API on their behalf (mwmbl.social).
"""

from dataclasses import dataclass, field
from logging import getLogger

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import HttpResponse
from django.utils import timezone
from ninja import Router
from ninja_jwt.authentication import JWTAuth

from mwmbl import social
from mwmbl.exceptions import InvalidRequest
from mwmbl.models import SocialRegistration
from mwmbl.platform.schemas import SocialError, SocialRegistrationRequest, SocialRegistrationResponse

logger = getLogger(__name__)

router = Router(tags=["Social"])

REFUSALS = {
    400: (
        "invalid_address",
        "Mwmbl Social can't use that address. Enter your server's https address, for example https://mastodon.example.",
    ),
    409: (
        "already_registered",
        "That server is already connected to Mwmbl Social, or waiting for its admin to approve the connection. "
        "Its admin can find it under Administration > Fediverse Auxiliary Service Providers.",
    ),
    502: (
        "server_unreachable",
        "Mwmbl Social couldn't register with that server: it couldn't be reached, isn't FASP-enabled, or refused. "
        "Mastodon needs version 4.5 or later with EXPERIMENTAL_FEATURES=fasp set. If the server was connected "
        "before, its admin must first delete the old Mwmbl Social provider: Mastodon refuses a second "
        "registration until then.",
    ),
}
_https_only = URLValidator(schemes=["https"])


@dataclass
class LiveStatus:
    status: str
    capabilities: list[str] = field(default_factory=list)


class Refused(Exception):
    def __init__(self, status: int, error: SocialError):
        super().__init__(error.message)
        self.status = status
        self.error = error


@router.post(
    "/registrations",
    auth=JWTAuth(),
    response={201: SocialRegistrationResponse, frozenset({400, 409, 429, 502, 503}): SocialError},
    summary="Connect a fediverse server",
    description="Registers the server with Mwmbl Social. Show the user the `fingerprint` and `approval_url`: the "
    "server's admin approves the connection there, then enables `data_sharing`, `account_search` and "
    "`follow_recommendation`. `approval_url` is only known now, but later reads return it too. Each user has "
    f"{settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY} attempts a day, and each server one attempt an hour "
    "whoever makes it, because each attempt leaves a pending provider in the server's admin panel: 429s carry "
    "`retry_after` and a Retry-After header.",
)
def create_registration(request, body: SocialRegistrationRequest, response: HttpResponse):
    try:
        server_url, host = social.server_url_and_host(body.server_url.strip())
    except ValueError as error:
        return 400, SocialError(code="invalid_address", message=str(error))
    try:
        registration, created = _register(request.user, server_url, host)
    except Refused as refused:
        if refused.error.retry_after is not None:
            response["Retry-After"] = str(refused.error.retry_after)
        return refused.status, refused.error
    return 201, _response(registration, created)


def _register(user, server_url: str, host: str) -> tuple[SocialRegistration, LiveStatus]:
    user_wait = social.user_attempt_wait(user.id)
    if user_wait is not None:
        limit = settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY
        message = f"You can try to connect {limit} servers a day. Try again tomorrow."
        raise Refused(429, SocialError(code="user_limit", message=message, retry_after=user_wait))
    host_wait = social.host_attempt_wait(host)
    if host_wait is not None:
        message = "Someone tried to connect that server in the last hour. Check its admin panel, or try again later."
        raise Refused(429, SocialError(code="host_limit", message=message, retry_after=host_wait))
    try:
        created = social.register(server_url)
    except social.RegistrationRefused as refused:
        code, message = REFUSALS[refused.status]
        raise Refused(refused.status, SocialError(code=code, message=message)) from refused
    except httpx.TransportError as error:
        # The registration may still have gone through, leaving a pending provider whose
        # approval link we never saw.
        logger.exception("Mwmbl Social did not answer a registration of %s", server_url)
        message = (
            "Mwmbl Social didn't answer in time. If your server's admin panel now lists Mwmbl Social, delete it "
            "there before trying again."
        )
        raise Refused(503, SocialError(code="social_unavailable", message=message)) from error

    approval_url = created["approval_url"]
    try:
        _https_only(approval_url)
    except ValidationError:
        approval_url = ""
    registration = SocialRegistration.objects.create(
        user=user,
        server_url=server_url,
        registration_id=created["id"],
        base_url=created["base_url"],
        fingerprint=created["fingerprint"],
        approval_url=approval_url,
    )
    return registration, LiveStatus(status=created["status"], capabilities=created["capabilities"])


@router.get(
    "/registrations",
    auth=JWTAuth(),
    response=list[SocialRegistrationResponse],
    summary="List your connected servers",
    description="The servers the user connected, newest first, with their live status from Mwmbl Social. If "
    "Mwmbl Social doesn't answer, rows that aren't disconnected have status `unavailable`.",
)
def list_registrations(request):
    registrations = list(request.user.social_registrations.order_by("-created_at"))
    live = _live_statuses(registrations)
    return [_response(registration, _live_status(registration, live)) for registration in registrations]


@router.get(
    "/registrations/{registration_id}",
    auth=JWTAuth(),
    response=SocialRegistrationResponse,
    summary="Get a connected server",
    description="One of the user's connected servers, as in the list. 404 if it doesn't exist or belongs to "
    "another user.",
)
def get_registration(request, registration_id: int):
    registration = _own_registration(request.user, registration_id)
    live = _live_statuses([registration])
    return _response(registration, _live_status(registration, live))


@router.delete(
    "/registrations/{registration_id}",
    auth=JWTAuth(),
    response={204: None},
    summary="Disconnect a server",
    description="Revokes the server's registration with Mwmbl Social, so it stops answering the server, and "
    "keeps the record as `disconnected`. Disconnecting one that is already disconnected does nothing. Tell the "
    "user to also delete Mwmbl Social from their server's admin panel: Mastodon refuses to connect it again "
    "until they do. 404 if it doesn't exist or belongs to another user.",
)
def delete_registration(request, registration_id: int):
    registration = _own_registration(request.user, registration_id)
    if registration.disconnected_at is None:
        social.revoke(registration.registration_id)
        registration.disconnected_at = timezone.now()
        registration.save(update_fields=["disconnected_at"])
    return 204, None


def _own_registration(user, registration_id: int) -> SocialRegistration:
    registration = SocialRegistration.objects.filter(user=user, id=registration_id).first()
    if registration is None:
        raise InvalidRequest("Registration not found.", status=404)
    return registration


def _live_statuses(registrations: list[SocialRegistration]) -> dict[int, dict] | None:
    """Mwmbl Social's registrations by id, in one request, or None if it didn't answer."""
    if all(registration.disconnected_at for registration in registrations):
        return {}
    try:
        return social.registrations_by_id()
    except httpx.HTTPError:
        logger.exception("Could not read registration statuses from Mwmbl Social")
        return None


def _live_status(registration: SocialRegistration, live: dict[int, dict] | None) -> LiveStatus:
    if registration.disconnected_at:
        return LiveStatus(status="disconnected")
    if live is None:
        return LiveStatus(status="unavailable")
    if registration.registration_id not in live:
        # Replaced by a newer registration of the same server.
        return LiveStatus(status="replaced")
    current = live[registration.registration_id]
    return LiveStatus(status=current["status"], capabilities=current["capabilities"])


def _response(registration: SocialRegistration, live: LiveStatus) -> SocialRegistrationResponse:
    return SocialRegistrationResponse(
        id=registration.id,
        server_url=registration.server_url,
        base_url=registration.base_url,
        status=live.status,
        capabilities=live.capabilities,
        fingerprint=registration.fingerprint,
        approval_url=registration.approval_url or None,
        created_at=registration.created_at,
        disconnected_at=registration.disconnected_at,
    )
