"""Tests for the search traffic counters and the middleware that writes them.

Counting is off in mwmbl.settings_test, because it is the only thing in the request path
that reaches for Redis and the CI job runs none. Everything here turns it back on and wires
in a fakeredis, the way test_admin_blacklist_status.py does.
"""

from datetime import timedelta

import fakeredis
import pytest
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.core.handlers.base import BaseHandler
from django.http import StreamingHttpResponse
from django.test import override_settings
from redis.exceptions import ConnectionError as RedisConnectionError

from mwmbl import traffic
from mwmbl.traffic_middleware import SearchTrafficMiddleware
from mwmbl.utils import utc_today

BROWSER_HEADERS = {
    "HTTP_USER_AGENT": "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "HTTP_ACCEPT_LANGUAGE": "en-GB,en;q=0.9",
}
FIREFOX = BROWSER_HEADERS["HTTP_USER_AGENT"]

_PROBE_HANDLER_WAS_ASYNC = []


class ModeProbeMiddleware:
    """Reports whether Django gave it an async handler, from where our middleware sits."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        handler_is_async = iscoroutinefunction(get_response)
        _PROBE_HANDLER_WAS_ASYNC.append(handler_is_async)
        if handler_is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        return self.get_response(request)


@pytest.fixture
def counting(monkeypatch, settings):
    settings.SEARCH_TRAFFIC_COUNTING = True
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(traffic, "_redis", client)
    return client


def count_for(endpoint, headers="user_agent+language"):
    key = traffic.REQUEST_COUNT_KEY.format(date=utc_today(), endpoint=endpoint, headers=headers)
    return traffic.get_redis().get(key)


def user_agent_counts():
    key = traffic.USER_AGENT_COUNT_KEY.format(date=utc_today())
    return dict(traffic.get_redis().zrange(key, 0, -1, withscores=True))


def test_the_middleware_is_installed_after_the_security_middleware():
    assert settings.MIDDLEWARE.index("mwmbl.traffic_middleware.SearchTrafficMiddleware") == (
        settings.MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1
    )


def test_the_chain_runs_sync_because_of_allauth():
    """Why SearchTrafficMiddleware has no async path.

    allauth's AccountMiddleware is sync-only, so Django adapts every middleware above it to
    sync even under ASGI. If this fails, allauth has gained async support, this middleware
    is now the thing forcing the chain sync, and it should grow an async branch.
    """
    probe_position = settings.MIDDLEWARE.index("mwmbl.traffic_middleware.SearchTrafficMiddleware")
    middleware = list(settings.MIDDLEWARE)
    # By __name__: test/ has no __init__.py, so a dotted path would import a second copy of
    # this module with its own module-level state, and the probe would record nothing.
    middleware.insert(probe_position, f"{__name__}.ModeProbeMiddleware")

    _PROBE_HANDLER_WAS_ASYNC.clear()
    with override_settings(MIDDLEWARE=middleware):
        BaseHandler().load_middleware(is_async=True)

    assert _PROBE_HANDLER_WAS_ASYNC == [False]


@pytest.mark.parametrize(
    "path,query,expected_endpoint",
    [
        ("/", {"q": "python"}, "ui_search"),
        ("/app/home/", {"q": "pyth"}, "ui_keystroke"),
        ("/app/home/", {"q": "python", "external": "1"}, "ui_search"),
        ("/api/v1/search/", {"s": "python"}, "v1_search"),
        ("/api/v2/search/", {"q": "python"}, "v2_search"),
        ("/api/v2/search/complete", {"q": "pyth"}, "v2_complete"),
        ("/search/", {"s": "python"}, "legacy_search"),
    ],
)
def test_each_kind_of_search_lands_in_its_own_bucket(counting, client, path, query, expected_endpoint):
    client.get(path, query, **BROWSER_HEADERS)

    assert count_for(expected_endpoint) == "1"
    others = [label for label in traffic.ALL_ENDPOINT_LABELS if label != expected_endpoint]
    assert all(count_for(label) is None for label in others)


@pytest.mark.django_db
@pytest.mark.parametrize("path,query", [("/", {}), ("/app/home/", {}), ("/api/v1/docs", {})])
def test_requests_that_are_not_searches_are_not_counted(counting, client, path, query):
    client.get(path, query, **BROWSER_HEADERS)

    assert counting.keys("traffic:*") == []


def test_every_search_route_has_a_label():
    """A renamed route must fail here rather than going silently uncounted."""
    from django.urls import get_resolver

    def view_names(resolver, namespace=None):
        for pattern in resolver.url_patterns:
            if hasattr(pattern, "url_patterns"):
                yield from view_names(pattern, pattern.namespace or namespace)
            elif pattern.name:
                yield f"{namespace}:{pattern.name}" if namespace else pattern.name

    searchy = {
        name
        for name in view_names(get_resolver())
        if name.split(":")[-1] in {"search", "complete", "raw", "super_search"}
        or name in {traffic.UI_INDEX_VIEW_NAME, traffic.UI_FRAGMENT_VIEW_NAME}
    }
    labelled = set(traffic.ENDPOINT_LABELS) | {traffic.UI_INDEX_VIEW_NAME, traffic.UI_FRAGMENT_VIEW_NAME}

    assert searchy - labelled == set()


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"HTTP_USER_AGENT": FIREFOX, "HTTP_ACCEPT_LANGUAGE": "en-GB"}, "user_agent+language"),
        ({"HTTP_USER_AGENT": FIREFOX}, "user_agent"),
        ({"HTTP_ACCEPT_LANGUAGE": "en-GB"}, "language"),
        ({}, "neither"),
    ],
)
def test_the_headers_that_arrived_are_counted_and_nothing_is_inferred_from_them(counting, client, headers, expected):
    client.get("/api/v1/search/", {"s": "python"}, **headers)

    assert count_for("v1_search", expected) == "1"


def test_the_counters_expire(counting, client):
    client.get("/api/v1/search/", {"s": "python"}, **BROWSER_HEADERS)

    requests_key = traffic.REQUEST_COUNT_KEY.format(
        date=utc_today(), endpoint="v1_search", headers="user_agent+language"
    )
    user_agents_key = traffic.USER_AGENT_COUNT_KEY.format(date=utc_today())
    assert 0 < counting.ttl(requests_key) <= traffic.TRAFFIC_EXPIRE_SECONDS
    assert 0 < counting.ttl(user_agents_key) <= traffic.USER_AGENT_EXPIRE_SECONDS


def test_a_user_agent_is_counted_verbatim(counting, client):
    client.get("/api/v1/search/", {"s": "python"}, **BROWSER_HEADERS)
    client.get("/api/v1/search/", {"s": "python"}, HTTP_USER_AGENT="Googlebot/2.1")

    assert user_agent_counts() == {FIREFOX: 1, "Googlebot/2.1": 1}


@pytest.mark.parametrize(
    "sent,counted",
    [
        # A missing user agent is what the front end's own server-side render sends, so it
        # is a number we want rather than a row to drop.
        (None, traffic.MISSING_USER_AGENT),
        ("A" * 2000, "A" * traffic.MAX_USER_AGENT_LENGTH),
    ],
)
def test_an_awkward_user_agent_still_gets_a_row(counting, client, sent, counted):
    headers = {"HTTP_USER_AGENT": sent} if sent else {}
    client.get("/api/v1/search/", {"s": "python"}, **headers)

    assert user_agent_counts() == {counted: 1}


def test_the_busiest_user_agents_survive_the_trim(counting, client, monkeypatch):
    """The bound on the key, and the reason the tail is not kept."""
    monkeypatch.setattr(traffic, "TRACKED_USER_AGENTS", 2)
    monkeypatch.setattr(traffic, "USER_AGENT_TRIM_PROBABILITY", 0)
    for user_agent, requests in [("busiest", 3), ("second", 2), ("seen once", 1)]:
        for _ in range(requests):
            client.get("/api/v1/search/", {"s": "python"}, HTTP_USER_AGENT=user_agent)

    monkeypatch.setattr(traffic, "USER_AGENT_TRIM_PROBABILITY", 1)
    client.get("/api/v1/search/", {"s": "python"}, HTTP_USER_AGENT="busiest")

    assert user_agent_counts() == {"busiest": 4, "second": 2}


def test_the_readout_sums_user_agents_across_the_window(counting, client):
    yesterday = utc_today() - timedelta(days=1)
    counting.zincrby(traffic.USER_AGENT_COUNT_KEY.format(date=yesterday), 5, FIREFOX)
    client.get("/api/v1/search/", {"s": "python"}, **BROWSER_HEADERS)

    assert traffic.read_user_agent_counts(counting, [yesterday, utc_today()], 10) == [(FIREFOX, 6)]


def test_the_readout_returns_a_count_per_day_endpoint_and_header_combination(counting, client):
    client.get("/api/v1/search/", {"s": "python"}, **BROWSER_HEADERS)
    client.get("/api/v1/search/", {"s": "python"}, **BROWSER_HEADERS)
    client.get("/api/v2/search/", {"q": "python"})

    counts = traffic.read_request_counts(counting, [utc_today()])

    assert counts == {
        (utc_today(), "v1_search", "user_agent+language"): 2,
        (utc_today(), "v2_search", "neither"): 1,
    }


def test_the_readout_survives_a_day_with_no_traffic(counting):
    days = [utc_today() - timedelta(days=offset) for offset in range(3)]

    assert traffic.read_request_counts(counting, days) == {}
    assert traffic.read_user_agent_counts(counting, days, 10) == []


def test_a_dead_redis_does_not_change_the_response(client, monkeypatch, settings):
    class DeadRedis:
        def pipeline(self):
            raise RedisConnectionError("Connection refused")

    with_counting_off = client.get("/api/v1/search/", {"s": "python"}, **BROWSER_HEADERS)

    settings.SEARCH_TRAFFIC_COUNTING = True
    monkeypatch.setattr(traffic, "_redis", DeadRedis())
    with_dead_redis = client.get("/api/v1/search/", {"s": "python"}, **BROWSER_HEADERS)

    assert with_dead_redis.status_code == with_counting_off.status_code == 200
    assert with_dead_redis.content == with_counting_off.content


def test_a_streaming_response_is_counted_without_being_consumed(counting, rf):
    """The Super Search view streams; touching response.content would raise on it."""
    request = rf.get("/api/v2/super-search/", {"q": "python"}, **BROWSER_HEADERS)
    request.resolver_match = type("Match", (), {"view_name": "api-v2:super_search"})()

    response = SearchTrafficMiddleware(lambda _: StreamingHttpResponse(iter([b"a", b"b"])))(request)

    assert count_for("super_search") == "1"
    assert b"".join(response.streaming_content) == b"ab"
