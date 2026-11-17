"""Connecting fediverse servers to Mwmbl Social, Mwmbl's hosted FASP at social.mwmbl.org.

Mwmbl calls the instance's admin API server-side, with an admin token, on behalf of a logged-in
user (see PLAN.md, "Deployment and adoption", in github.com/mwmbl/social). There is no
moderation queue: the server admin approving the registration on their own server is what
proves consent. The rate limits here are what stop anyone filling other servers' admin panels
with pending registrations.
"""

from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.cache import cache

DAY_SECONDS = 24 * 60 * 60
# The answers that are about the server the user named rather than about us: an unusable URL,
# a server already connected, and one that can't be reached or refuses. Anything else means
# Mwmbl or Mwmbl Social is misconfigured, so it raises.
REFUSAL_STATUSES = (400, 409, 502)


class RegistrationRefused(Exception):
    def __init__(self, status: int):
        super().__init__(f"Mwmbl Social refused the registration with {status}")
        self.status = status


def server_url_and_host(entered: str) -> tuple[str, str]:
    """`entered` as https://host[:port], and that host[:port], or ValueError saying what is wrong.

    A bare host name is taken to mean https, since that is all Mwmbl Social accepts.
    """
    with_scheme = entered if "://" in entered else f"https://{entered}"
    parts = urlsplit(with_scheme)
    if parts.scheme != "https":
        raise ValueError("Enter your server's https address.")
    if not parts.hostname or parts.username is not None:
        raise ValueError("Enter your server's address, for example https://mastodon.example.")
    host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return f"https://{host}", host


def take_user_attempt(user_id: int) -> bool:
    """Count an attempt by this user, and say whether it is within their daily limit."""
    key = f"social:connect:user:{user_id}"
    cache.add(key, 0, timeout=DAY_SECONDS)
    return cache.incr(key) <= settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY


def take_host_attempt(host: str) -> bool:
    """Whether nobody has tried to connect this host recently, marking that someone now has."""
    return cache.add(f"social:connect:host:{host}", 1, timeout=settings.SOCIAL_CONNECT_SECONDS_BETWEEN_HOST_ATTEMPTS)


def _registrations_url() -> str:
    return f"{settings.MWMBL_SOCIAL_URL}/api/v1/admin/registrations"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.MWMBL_SOCIAL_ADMIN_TOKEN}"}


def register(server_url: str) -> dict:
    """Register the server, returning Mwmbl Social's registration, with its approval_url."""
    response = httpx.post(
        _registrations_url(),
        json={"server_url": server_url},
        headers=_headers(),
        timeout=settings.SOCIAL_REGISTER_TIMEOUT_SECONDS,
    )
    if response.status_code in REFUSAL_STATUSES:
        raise RegistrationRefused(response.status_code)
    response.raise_for_status()
    return response.json()


def registrations_by_id() -> dict[int, dict]:
    """Every registration Mwmbl Social has, by id: one request however many the page shows."""
    response = httpx.get(_registrations_url(), headers=_headers(), timeout=settings.SOCIAL_API_TIMEOUT_SECONDS)
    response.raise_for_status()
    return {registration["id"]: registration for registration in response.json()}


def revoke(registration_id: int) -> None:
    response = httpx.delete(
        f"{_registrations_url()}/{registration_id}", headers=_headers(), timeout=settings.SOCIAL_API_TIMEOUT_SECONDS
    )
    # 404: it is already gone, replaced by a newer registration of the same server.
    if response.status_code != 404:
        response.raise_for_status()
