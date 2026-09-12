"""Counting search traffic, and assuming nothing about it.

Phase 0 of #410: before we can decide what an anonymous visitor is allowed to cost us, we
need to know how much of the traffic is people. This counts; nothing acts on the answer and
no request's behaviour changes.

Deliberately raw. Classifying a request as a bot or a browser here would fix a taxonomy in
the Redis keys before anyone has looked at the traffic, and a key dimension is not something
you can change your mind about later. What is counted is what arrived: requests per
endpoint, split by which of the two headers were present, and requests per verbatim user
agent. Any rule about what a bot looks like then applies when the numbers are read, over
strings we already have, and can be applied differently next week. #412 is where a
classification earns its place, once there is data to design it against.
"""

import random
from collections import Counter
from datetime import date
from logging import getLogger
from typing import Optional

import redis
from django.conf import settings
from django.http import HttpRequest
from redis.exceptions import RedisError

from mwmbl.utils import utc_today

logger = getLogger(__name__)

# nginx accepts an 8 KB header; this bounds what one request can make us store.
MAX_USER_AGENT_LENGTH = 512

# Endpoint labels. Keyed on resolver_match.view_name rather than request.path. The names are unique across
# the two ninja APIs and the legacy one because each mounts under its own namespace, and
# they are bounded by construction - a path-derived label would grow keys from a string the
# caller chooses. Anything not in here is not search traffic and is not counted.
ENDPOINT_LABELS = {
    "search-0.1:search": "legacy_search",
    "search-0.1:complete": "legacy_complete",
    "search-0.1:raw": "legacy_raw",
    "api-v1:search": "v1_search",
    "api-v1:complete": "v1_complete",
    "api-v1:raw": "v1_raw",
    "api-v2:search": "v2_search",
    "api-v2:complete": "v2_complete",
    "api-v2:raw": "v2_raw",
    "api-v2:super_search": "super_search",
}

# The Django UI is two views and three kinds of request, so it cannot be a straight lookup.
UI_INDEX_VIEW_NAME = "index"
UI_FRAGMENT_VIEW_NAME = "home"
UI_SEARCH_LABEL = "ui_search"
UI_KEYSTROKE_LABEL = "ui_keystroke"

ALL_ENDPOINT_LABELS = sorted(set(ENDPOINT_LABELS.values()) | {UI_SEARCH_LABEL, UI_KEYSTROKE_LABEL})


def _ui_endpoint_label(request: HttpRequest) -> Optional[str]:
    """Which of the Django UI's three kinds of request this is.

    Without the keystroke split the UI's search count is inflated roughly tenfold:
    home_fragment is hit on every keystroke and only the debounced call carries external=1.
    """
    if not request.GET.get("q"):
        return None
    if request.resolver_match.view_name == UI_INDEX_VIEW_NAME:
        return UI_SEARCH_LABEL
    return UI_SEARCH_LABEL if request.GET.get("external") == "1" else UI_KEYSTROKE_LABEL


def endpoint_label(request: HttpRequest) -> Optional[str]:
    """The counter label for this request, or None if it is not search traffic.

    resolver_match is populated by the time a response-phase middleware runs, but is None
    for a 404 and for CommonMiddleware's APPEND_SLASH redirect, both of which this sits
    outside of.
    """
    if request.resolver_match is None:
        return None
    view_name = request.resolver_match.view_name
    if view_name in (UI_INDEX_VIEW_NAME, UI_FRAGMENT_VIEW_NAME):
        return _ui_endpoint_label(request)
    return ENDPOINT_LABELS.get(view_name)


# The one split on the request counter, and a description rather than a judgement: which of
# the two headers arrived, not what that means. A request with neither is what our own
# server-side render sends today, and a missing Accept-Language is the cheapest hint that a
# client is not a browser, since browsers send one and HTTP clients do not.
HEADER_LABELS = ["user_agent+language", "user_agent", "language", "neither"]


def header_label(request: HttpRequest) -> str:
    has_user_agent = bool(request.headers.get("User-Agent"))
    has_language = bool(request.headers.get("Accept-Language"))
    if has_user_agent:
        return "user_agent+language" if has_language else "user_agent"
    return "language" if has_language else "neither"


REQUEST_COUNT_KEY = "traffic:req:{date}:{endpoint}:{headers}"
USER_AGENT_COUNT_KEY = "traffic:user-agents:{date}"

# Thirty days, matching the public daily crawler counters in mwmbl/crawler/stats.py.
# Production Redis runs allkeys-lru (see mwmbl/indexer/blacklist_snapshot.py), so a longer
# retention would be a promise the eviction policy does not keep.
TRAFFIC_EXPIRE_SECONDS = 60 * 60 * 24 * 30

# Seven days for the user agents: they are here to be read and argued about, not to be a
# permanent series, and a short window limits what a verbatim string can later be used for.
USER_AGENT_EXPIRE_SECONDS = 60 * 60 * 24 * 7

# How many user agents a day survives, which is both the memory bound on a sorted set whose
# members the caller chooses and the privacy control. What it drops is the tail, and a user
# agent seen once is both most of a browser fingerprint and a lone person; what survives is
# by construction a client somebody runs at volume.
TRACKED_USER_AGENTS = 2000

# Trimming on one request in five hundred holds the set within a few hundred rows of the
# limit. There is no counter shared between workers to do it on a schedule, and a ZCARD to
# decide would cost a round trip on every request.
USER_AGENT_TRIM_PROBABILITY = 1 / 500

# A missing user agent is a real answer, so it gets a row rather than being dropped.
MISSING_USER_AGENT = "(none)"

# Aggressive by design. django-redis is configured with 5 s timeouts, which is fine for a
# cache lookup a view is waiting on and far too slow for a counter: this runs on every
# request, on the one thread asgiref gives the whole sync middleware chain.
REDIS_CONNECT_TIMEOUT_SECONDS = 0.1
REDIS_TIMEOUT_SECONDS = 0.25

_redis: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            decode_responses=True,
        )
    return _redis


def record_request(request: HttpRequest) -> None:
    """Count one search request. Never raises: a counter that costs a search its results is
    worse than no counter, which is the rule external_cache already follows."""
    if not settings.SEARCH_TRAFFIC_COUNTING:
        return

    endpoint = endpoint_label(request)
    if endpoint is None:
        return

    today = utc_today()
    try:
        pipeline = get_redis().pipeline()

        request_key = REQUEST_COUNT_KEY.format(date=today, endpoint=endpoint, headers=header_label(request))
        pipeline.incr(request_key)
        pipeline.expire(request_key, TRAFFIC_EXPIRE_SECONDS)

        # Its own key, not a dimension on the counter above: nothing joins a user agent to
        # an address or a query, so no row says a particular person searched for something.
        user_agent_key = USER_AGENT_COUNT_KEY.format(date=today)
        user_agent = request.headers.get("User-Agent", "")[:MAX_USER_AGENT_LENGTH] or MISSING_USER_AGENT
        pipeline.zincrby(user_agent_key, 1, user_agent)
        pipeline.expire(user_agent_key, USER_AGENT_EXPIRE_SECONDS)
        if random.random() < USER_AGENT_TRIM_PROBABILITY:
            pipeline.zremrangebyrank(user_agent_key, 0, -TRACKED_USER_AGENTS - 1)

        pipeline.execute()
    except RedisError:
        logger.warning("Could not record search traffic for %s", endpoint, exc_info=True)


def read_request_counts(redis_client, days: list[date]) -> dict[tuple[date, str, str], int]:
    """Request counts for the given days, keyed by (day, endpoint, header label).

    One mget over the known labels rather than a scan: nothing indexes which traffic keys
    exist, and SCAN on a shared Redis is not something a readout should do. Combinations
    with no traffic are absent rather than zero.
    """
    coordinates = [
        (day, endpoint, headers) for day in days for endpoint in ALL_ENDPOINT_LABELS for headers in HEADER_LABELS
    ]
    counts = redis_client.mget(
        [
            REQUEST_COUNT_KEY.format(date=day, endpoint=endpoint, headers=headers)
            for day, endpoint, headers in coordinates
        ]
    )
    return {coordinate: int(count) for coordinate, count in zip(coordinates, counts) if count}


def read_user_agent_counts(redis_client, days: list[date], limit: int) -> list[tuple[str, int]]:
    """The most-seen user agents over the window, most requests first.

    Summed across days, so a client that appears every day outranks one that appeared once.
    """
    pipeline = redis_client.pipeline()
    for day in days:
        pipeline.zrevrange(USER_AGENT_COUNT_KEY.format(date=day), 0, TRACKED_USER_AGENTS, withscores=True)

    totals: Counter = Counter()
    for day_counts in pipeline.execute():
        for user_agent, count in day_counts:
            totals[user_agent] += int(count)
    return totals.most_common(limit)
