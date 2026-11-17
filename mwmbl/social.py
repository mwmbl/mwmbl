"""Mwmbl Social, Mwmbl's hosted FASP at social.mwmbl.org: connecting fediverse servers to it,
and searching the posts it has indexed.

Mwmbl calls the instance's admin API server-side, with an admin token, on behalf of a logged-in
user (see PLAN.md, "Deployment and adoption", in github.com/mwmbl/social). There is no
moderation queue: the server admin approving the registration on their own server is what
proves consent. The rate limits here are what stop anyone filling other servers' admin panels
with pending registrations.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

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
# Profile paths of Mastodon and its forks (/users/name, /@name) and Lemmy (/u/name).
PROFILE_PATH = re.compile(r"/(?:users/|u/|@)([^/@]+)/?")


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


@dataclass(frozen=True)
class SocialPost:
    """A post as the social search page shows it. A link is None unless it is http(s): posts and
    their authors' addresses come from servers we don't control."""

    url: str | None
    author_url: str | None
    author: str
    title: str | None
    extract: str
    published: datetime


def search_posts(query: str) -> list[SocialPost]:
    """Mwmbl Social's posts matching `query`, best first.

    Results are cached for SOCIAL_SEARCH_CACHE_SECONDS and never stored anywhere else: Mwmbl
    Social hides a deleted or withdrawn post at once, and a copy here would outlive that.
    """
    key = f"social:search:posts:{hashlib.sha256(query.encode()).hexdigest()}"
    found = cache.get(key)
    if found is None:
        response = httpx.get(
            f"{settings.MWMBL_SOCIAL_URL}/api/v1/search/posts",
            params={"q": query},
            timeout=settings.SOCIAL_SEARCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        found = response.json()
        cache.set(key, found, timeout=settings.SOCIAL_SEARCH_CACHE_SECONDS)
    return [_post(item) for item in found]


def _post(item: dict) -> SocialPost:
    return SocialPost(
        url=_http_url(item["uri"]),
        author_url=_http_url(item["author_uri"]),
        author=author_name(item["author_uri"]),
        title=item.get("title"),
        extract=item["extract"],
        published=datetime.fromtimestamp(item["published"], tz=UTC),
    )


def _http_url(url: str) -> str | None:
    # Checked on the raw string, as a browser reads it: anything else, javascript: included,
    # must never become a link.
    return url if url.startswith(("https://", "http://")) else None


def author_name(author_uri: str) -> str:
    """The author's handle, @name@host, where their profile address follows a known pattern, and
    otherwise the address without its scheme. The search API gives only the author's URI."""
    try:
        url = httpx.URL(author_uri)
    except httpx.InvalidURL:
        return author_uri
    profile = PROFILE_PATH.fullmatch(url.path)
    if profile is None or not url.host:
        return author_uri.removeprefix("https://").removeprefix("http://")
    return f"@{profile.group(1)}@{url.host}"
