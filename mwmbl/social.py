"""Connecting fediverse servers to Mwmbl Social, Mwmbl's hosted FASP at social.mwmbl.org.

Mwmbl calls the instance's admin API server-side, with an admin token, on behalf of a logged-in
user (see PLAN.md, "Deployment and adoption", in github.com/mwmbl/social). There is no
moderation queue: the server admin approving the registration on their own server is what
proves consent. The rate limits here are what stop anyone filling other servers' admin panels
with pending registrations.
"""

import re
import time

import httpx
from django.conf import settings
from django.core.cache import cache

DAY_SECONDS = 24 * 60 * 60
# The answers that are about the server the user named rather than about us: an unusable URL,
# a server already connected, and one that can't be reached or refuses. Anything else means
# Mwmbl or Mwmbl Social is misconfigured, so it raises.
REFUSAL_STATUSES = (400, 409, 502)
ADDRESS_EXAMPLE = "Enter your server's domain name, for example https://mastodon.example."
# Every top-level domain starts with a letter, punycode ones (xn--) included.
TOP_LEVEL_LABEL = re.compile(r"[a-z][a-z0-9-]*")


class RegistrationRefused(Exception):
    def __init__(self, status: int):
        super().__init__(f"Mwmbl Social refused the registration with {status}")
        self.status = status


def server_url_and_host(entered: str) -> tuple[str, str]:
    """`entered` as https://host[:port], and that host[:port], or ValueError saying what is wrong.

    A bare host name is taken to mean https, since that is all Mwmbl Social accepts. The host is
    canonical (lower case, punycode, no trailing dot, no :443), because it keys the per-server
    rate limit: spellings of one server must not each get their own attempt.
    """
    with_scheme = entered if "://" in entered else f"https://{entered}"
    try:
        url = httpx.URL(with_scheme)
    except httpx.InvalidURL as error:
        raise ValueError(ADDRESS_EXAMPLE) from error
    if url.scheme != "https":
        raise ValueError("Enter your server's https address.")
    if not url.host or url.userinfo:
        raise ValueError(ADDRESS_EXAMPLE)
    hostname = url.raw_host.decode("ascii").rstrip(".")
    # Mwmbl Social fetches whatever we send it from inside our network, so only a public domain
    # name will do: no IP address (a URL parser reads any host whose last label is numeric as
    # IPv4) and no single-label name such as localhost or a container's.
    *_, top_level_label = hostname.split(".")
    if "." not in hostname or not TOP_LEVEL_LABEL.fullmatch(top_level_label):
        raise ValueError(ADDRESS_EXAMPLE)
    host = hostname if url.port is None else f"{hostname}:{url.port}"
    return f"https://{host}", host


def user_attempt_wait(user_id: int) -> int | None:
    """Count an attempt by this user: None if it is within their daily limit, otherwise the
    seconds until the day's window ends and they may try again."""
    now = int(time.time())
    window = now // DAY_SECONDS
    key = f"social:connect:user:{user_id}:{window}"
    cache.add(key, 0, timeout=DAY_SECONDS)
    if cache.incr(key) <= settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY:
        return None
    return (window + 1) * DAY_SECONDS - now


def host_attempt_wait(host: str) -> int | None:
    """Mark that someone is trying to connect this host: None if nobody has recently, otherwise
    the seconds until it may be tried again."""
    now = int(time.time())
    key = f"social:connect:host:{host}"
    window_seconds = settings.SOCIAL_CONNECT_SECONDS_BETWEEN_HOST_ATTEMPTS
    if cache.add(key, now, timeout=window_seconds):
        return None
    return max(cache.get(key, now) + window_seconds - now, 1)


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
