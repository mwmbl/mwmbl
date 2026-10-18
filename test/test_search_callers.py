"""Tests for the weekly distinct-caller counts (mwmbl.search_callers) and their admin readout.

Counting is off in mwmbl.settings_test, so these turn it back on and wire in a fakeredis,
as test_traffic.py does.
"""

from datetime import date, timedelta

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.http import HttpResponseServerError

from mwmbl import admin_views, background, search_callers, traffic
from mwmbl.utils import utc_today

User = get_user_model()

SEARCH = "/api/v1/search/"


@pytest.fixture
def counting(monkeypatch, settings):
    settings.SEARCH_TRAFFIC_COUNTING = True
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(traffic, "_redis", client)
    monkeypatch.setattr(search_callers, "_salts", {})
    return client


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user(username="staff_member_1", password="correctpassword", is_staff=True)
    client.force_login(user)
    return client


def this_window():
    return search_callers.window_start(utc_today())


def search(client, address, times=1):
    for _ in range(times):
        client.get(SEARCH, {"s": "python"}, REMOTE_ADDR=address)


def caller_counts(redis_client):
    key = search_callers.COUNT_KEY.format(window=this_window())
    return sorted(int(count) for _, count in redis_client.zrange(key, 0, -1, withscores=True))


def distinct_callers(redis_client):
    return redis_client.pfcount(search_callers.DISTINCT_KEY.format(window=this_window()))


def test_a_window_is_a_monday_to_sunday_week():
    monday = date(2026, 10, 12)

    assert [search_callers.window_start(monday + timedelta(days=offset)) for offset in range(8)] == [monday] * 7 + [
        monday + timedelta(days=7)
    ]


def test_each_address_is_one_caller(counting, client):
    search(client, "203.0.113.1", times=3)
    search(client, "203.0.113.2")

    assert distinct_callers(counting) == 2
    assert caller_counts(counting) == [1, 3]


def test_an_ipv6_caller_is_its_whole_64(counting, client):
    search(client, "2001:db8:1:2::1")
    search(client, "2001:db8:1:2:ffff::9")
    search(client, "2001:db8:1:3::1")

    assert caller_counts(counting) == [1, 2]


@pytest.mark.parametrize(
    "headers,expected",
    [
        # Earlier X-Forwarded-For entries are whatever the client claimed.
        ({"HTTP_X_FORWARDED_FOR": "10.0.0.1, 198.51.100.7", "REMOTE_ADDR": "172.17.0.1"}, "198.51.100.7"),
        ({"REMOTE_ADDR": "::ffff:198.51.100.7"}, "198.51.100.7"),
        ({"HTTP_X_FORWARDED_FOR": "unknown"}, "unknown"),
    ],
)
def test_the_caller_is_the_address_our_nginx_saw(rf, headers, expected):
    assert search_callers.client_network(rf.get("/", **headers)) == expected


def test_a_request_with_no_address_is_still_counted(counting, rf):
    """uvicorn on a unix socket gives Django no REMOTE_ADDR, and counting must not fail the search."""
    request = rf.get(SEARCH, {"s": "python"})
    del request.META["REMOTE_ADDR"]
    request.resolver_match = type("Match", (), {"view_name": "api-v1:search"})()

    traffic.record_request(request, type("Response", (), {"status_code": 200})())

    assert distinct_callers(counting) == 1


def test_no_address_is_stored_and_tokens_depend_on_the_window_salt(counting, client):
    search(client, "203.0.113.1")

    stored = counting.zrange(search_callers.COUNT_KEY.format(window=this_window()), 0, -1)
    assert stored and all("203.0.113" not in token for token in stored)
    salt = counting.get(search_callers.SALT_KEY.format(window=this_window()))
    assert stored == [search_callers.caller_token(salt, "203.0.113.1")]
    assert search_callers.caller_token("another week's salt", "203.0.113.1") != stored[0]


def test_an_evicted_salt_is_put_back_rather_than_replaced(counting, client):
    search(client, "203.0.113.1")
    salt_key = search_callers.SALT_KEY.format(window=this_window())
    salt = counting.get(salt_key)
    counting.delete(salt_key)

    search(client, "203.0.113.1")

    assert counting.get(salt_key) == salt
    assert caller_counts(counting) == [2]


@pytest.mark.parametrize(
    "path,query",
    [("/api/v1/search/complete", {"q": "pyth"}), ("/api/v1/search/raw", {"s": "python"}), ("/app/home/", {"q": "py"})],
)
def test_only_typed_searches_are_attributed_to_a_caller(counting, client, path, query):
    client.get(path, query)

    assert distinct_callers(counting) == 0


def test_an_unserved_search_is_not_attributed_to_a_caller(counting, rf):
    request = rf.get(SEARCH, {"s": "python"})
    request.resolver_match = type("Match", (), {"view_name": "api-v1:search"})()

    traffic.record_request(request, HttpResponseServerError())

    assert distinct_callers(counting) == 0


def test_the_per_caller_keys_die_a_day_after_the_week(counting, client):
    search(client, "203.0.113.1", times=10)

    expiry = search_callers._raw_expiry(this_window())
    assert expiry.date() == this_window() + timedelta(days=8)
    # Ten searches cross the first threshold only, so the higher thresholds' keys do not exist.
    written = [key for key in search_callers._raw_keys(this_window()) if counting.exists(key)]
    assert len(written) == 4
    assert all(counting.expiretime(key) == int(expiry.timestamp()) for key in written)


def test_callers_are_counted_as_they_cross_each_threshold(counting, client, monkeypatch):
    monkeypatch.setattr(search_callers, "THRESHOLDS", [2, 3])
    search(client, "203.0.113.1", times=3)
    search(client, "203.0.113.2", times=2)
    search(client, "203.0.113.3")

    totals = search_callers.read_live(counting, this_window(), searches=6)

    assert totals["over"] == [2, 1]


def test_the_busiest_callers_survive_the_trim(counting, client, monkeypatch):
    monkeypatch.setattr(search_callers, "TRACKED_CALLERS", 1)
    monkeypatch.setattr(search_callers, "TRIM_PROBABILITY", 0)
    search(client, "203.0.113.1", times=2)
    search(client, "203.0.113.2")
    monkeypatch.setattr(search_callers, "TRIM_PROBABILITY", 1)
    search(client, "203.0.113.1")

    assert caller_counts(counting) == [3]


def test_the_shares_are_of_every_served_search(counting, client):
    search(client, "203.0.113.1", times=3)
    search(client, "203.0.113.2")
    # A served search the trim would have dropped, and traffic that is not a typed search.
    counting.incrby(
        traffic.REQUEST_COUNT_KEY.format(date=utc_today(), endpoint="ui_search", headers="neither", status="2xx"), 4
    )
    client.get("/api/v1/search/raw", {"s": "python"})

    searches = traffic.caller_window_searches(counting, this_window())
    totals = search_callers.read_live(counting, this_window(), searches)

    assert searches == 8
    assert totals["busiest_searches"] == [3, 4, 4, 4]


def last_window_with(redis_client, searches_by_caller):
    """Seed last week's keys the way record_request would have written them."""
    last_window = this_window() - timedelta(days=7)
    for caller, searches in searches_by_caller.items():
        redis_client.pfadd(search_callers.DISTINCT_KEY.format(window=last_window), caller)
        redis_client.zincrby(search_callers.COUNT_KEY.format(window=last_window), searches, caller)
    redis_client.set(search_callers.SALT_KEY.format(window=last_window), "salt")
    redis_client.incrby(
        traffic.REQUEST_COUNT_KEY.format(date=last_window, endpoint="v1_search", headers="neither", status="2xx"),
        sum(searches_by_caller.values()),
    )
    return last_window


def test_a_finished_week_is_replaced_by_its_totals(counting):
    last_window = last_window_with(counting, {"instance": 90, "browser": 10})

    background.summarise_search_callers.now()

    assert all(not counting.exists(key) for key in search_callers._raw_keys(last_window))
    assert search_callers.read_summary(counting, last_window) == {
        "searches": 100,
        "distinct": 2,
        "busiest_searches": [90, 100, 100, 100],
        "over": [0, 0, 0],
    }
    assert 0 < counting.ttl(search_callers.SUMMARY_KEY.format(window=last_window))


def test_a_late_search_does_not_replace_the_summary(counting):
    last_window = last_window_with(counting, {"instance": 90})
    traffic.summarise_last_caller_window(counting)
    counting.pfadd(search_callers.DISTINCT_KEY.format(window=last_window), "straggler")

    assert traffic.summarise_last_caller_window(counting) is False
    assert search_callers.read_summary(counting, last_window)["distinct"] == 1


def test_this_week_is_not_summarised(counting, client):
    search(client, "203.0.113.1")

    assert traffic.summarise_last_caller_window(counting) is False
    assert distinct_callers(counting) == 1


def test_the_page_shows_this_week_live_and_finished_weeks_from_their_summaries(staff_client, counting, client):
    last_window = last_window_with(counting, {"instance": 90, "browser": 10})
    traffic.summarise_last_caller_window(counting)
    search(client, "203.0.113.1", times=2)

    response = staff_client.get("/admin/search-traffic/")

    assert response.status_code == 200
    assert "Distinct callers per week" in response.content.decode()
    rows = response.context["callers"]["rows"]
    assert [(row["window"], row["summarised"], row["distinct"]) for row in rows] == [
        (this_window(), False, 1),
        (last_window, True, 2),
    ]
    assert rows[0]["shares"][0] == 100
    assert rows[1]["shares"][0] == 90


def test_weeks_with_no_callers_are_left_out(staff_client, counting):
    response = staff_client.get("/admin/search-traffic/")

    assert response.context["callers"]["rows"] == []
    assert admin_views.CALLER_WINDOWS == 14
