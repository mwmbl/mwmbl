"""Tests for the search traffic admin page (mwmbl.admin_views.search_traffic_view).

Counting is off in mwmbl.settings_test, so the one test that seeds through the middleware's
own code path turns it back on, as test_traffic.py does. Everything else writes the keys
directly: record_request can only ever write today's.
"""

from datetime import timedelta

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from redis import ConnectionError as RedisConnectionError

from mwmbl import admin_views, traffic
from mwmbl.utils import utc_today

User = get_user_model()

TRAFFIC_URL = "/admin/search-traffic/"


@pytest.fixture
def redis_client(monkeypatch):
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(traffic, "_redis", client)
    return client


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user(username="staff_member_1", password="correctpassword", is_staff=True)
    client.force_login(user)
    return client


def record(redis_client, endpoint, day=None, headers="user_agent+language", status="2xx", count=1, user_agent=None):
    """Seed the counters the way record_request writes them, on any day."""
    day = day or utc_today()
    redis_client.incrby(
        traffic.REQUEST_COUNT_KEY.format(date=day, endpoint=endpoint, headers=headers, status=status), count
    )
    if user_agent is not None:
        redis_client.zincrby(traffic.USER_AGENT_COUNT_KEY.format(date=day), count, user_agent)


def window(days):
    """The days most recent first, and the counts over them, as the view reads them."""
    dates = [utc_today() - timedelta(days=offset) for offset in range(days)]
    return dates, traffic.read_request_counts(traffic.get_redis(), dates)


@pytest.mark.parametrize(
    "requested,expected", [("0", 1), ("-5", 1), ("1", 1), ("30", 30), ("999", 30), ("nonsense", 7), ("", 7)]
)
def test_days_is_clamped_to_what_is_retained(requested, expected):
    """A window wider than the expiry would show zeros for days that were never kept, which
    reads as no traffic rather than as no data."""
    request = RequestFactory().get(TRAFFIC_URL, {"days": requested})

    assert len(admin_views._traffic_days(request)) == expected


def test_requests_are_counted_per_endpoint_per_day(redis_client):
    yesterday = utc_today() - timedelta(days=1)
    record(redis_client, "v1_search", count=10)
    record(redis_client, "v1_search", day=yesterday, count=4)
    record(redis_client, "ui_search", count=3)

    days, counts = window(2)
    table = admin_views._requests_by_label(counts, admin_views.DAY, days)

    # Busiest first, the ten endpoints with no traffic absent rather than zero rows, and the
    # columns running from today backwards as _traffic_days orders them.
    assert [row["endpoint"] for row in table["rows"]] == ["v1_search", "ui_search"]
    assert len(traffic.ALL_ENDPOINT_LABELS) > 2
    assert table["rows"][0]["counts"] == [10, 4]
    assert table["rows"][0]["total"] == 14
    assert table["totals"] == [13, 4]
    assert table["total"] == 17


def test_the_splits_slice_the_same_requests(redis_client):
    """The header split and the status split are one set of requests counted two ways. If
    they disagree, one of them is dropping a dimension of the key."""
    record(redis_client, "v1_search", headers="neither", status="2xx", count=7)
    record(redis_client, "v1_search", headers="user_agent", status="4xx", count=2)
    record(redis_client, "ui_keystroke", headers="user_agent+language", status="2xx", count=5)

    days, counts = window(1)
    by_day = admin_views._requests_by_label(counts, admin_views.DAY, days)
    by_headers = admin_views._requests_by_label(counts, admin_views.HEADERS, traffic.HEADER_LABELS)
    by_status = admin_views._requests_by_label(counts, admin_views.STATUS, traffic.STATUS_LABELS)

    assert by_day["total"] == by_headers["total"] == by_status["total"] == 14
    assert dict(zip(traffic.HEADER_LABELS, by_headers["totals"]))["neither"] == 7
    assert dict(zip(traffic.STATUS_LABELS, by_status["totals"]))["4xx"] == 2


def test_the_page_reads_what_the_middleware_writes(redis_client, settings, rf):
    """The two sides label an endpoint in different places, so a request the middleware
    counts under a label the page does not read shows up here rather than as a silent zero."""
    settings.SEARCH_TRAFFIC_COUNTING = True
    request = rf.get("/app/home/", {"q": "test", "external": "1"}, HTTP_USER_AGENT="Firefox")
    request.resolver_match = type("Match", (), {"view_name": "home"})()

    traffic.record_request(request, type("Response", (), {"status_code": 200})())

    days, counts = window(1)
    table = admin_views._requests_by_label(counts, admin_views.DAY, days)
    assert [(row["endpoint"], row["total"]) for row in table["rows"]] == [("ui_search", 1)]


def test_user_agents_are_summed_across_days_and_ranked(redis_client):
    yesterday = utc_today() - timedelta(days=1)
    record(redis_client, "v1_search", count=3, user_agent="EveryDayBot/1.0")
    record(redis_client, "v1_search", day=yesterday, count=3, user_agent="EveryDayBot/1.0")
    record(redis_client, "v1_search", count=5, user_agent="OneOff/1.0")

    agents = admin_views._user_agents(traffic.get_redis(), *window(2))

    assert [agent["user_agent"] for agent in agents["top"]] == ["EveryDayBot/1.0", "OneOff/1.0"]
    assert agents["top"][0]["count"] == 6
    assert agents["top"][0]["share"] == pytest.approx(100 * 6 / 11)
    assert agents["tracked_total"] == 11


def test_the_agent_window_stops_at_the_agent_expiry(redis_client):
    """The two keys have different expiries, so a thirty day request window must not compare
    a week of user agents against a month of requests."""
    old_day = utc_today() - timedelta(days=admin_views.USER_AGENT_MAX_DAYS + 1)
    record(redis_client, "v1_search", count=10, user_agent="Known/1.0")
    record(redis_client, "v1_search", day=old_day, count=100)

    agents = admin_views._user_agents(traffic.get_redis(), *window(30))

    assert len(agents["days"]) == admin_views.USER_AGENT_MAX_DAYS
    assert agents["request_total"] == 10
    assert agents["gap"] == 0


def test_the_gap_is_the_traffic_no_tracked_agent_accounts_for(redis_client):
    record(redis_client, "v1_search", count=10, user_agent="Known/1.0")
    # A request counted on the same day whose agent the trim has since dropped.
    record(redis_client, "v1_search", headers="neither", count=4)

    agents = admin_views._user_agents(traffic.get_redis(), *window(1))

    assert (agents["request_total"], agents["tracked_total"], agents["gap"]) == (14, 10, 4)


def test_view_requires_staff(client, db):
    user = User.objects.create_user(username="ordinary_user_2", password="correctpassword")
    client.force_login(user)

    response = client.get(TRAFFIC_URL)

    assert response.status_code == 302
    assert "/admin/login/" in response.url


def test_view_renders_the_tables(staff_client, redis_client):
    record(redis_client, "v1_search", count=1234, user_agent="SomeBot/2.0")

    response = staff_client.get(TRAFFIC_URL)
    content = response.content.decode()

    assert response.status_code == 200
    assert "SomeBot/2.0" in content
    assert "1,234" in content
    assert response.context["by_day"]["total"] == 1234
    assert response.context["by_headers"]["total"] == 1234
    assert response.context["by_status"]["total"] == 1234


def test_view_renders_an_empty_window(staff_client, redis_client):
    response = staff_client.get(TRAFFIC_URL)

    assert response.status_code == 200
    assert "No requests counted in this window" in response.content.decode()


def test_view_escapes_user_agent_strings(staff_client, redis_client):
    """The one string on this page that a caller chooses."""
    record(redis_client, "v1_search", user_agent="<script>alert(1)</script>")

    content = staff_client.get(TRAFFIC_URL).content.decode()

    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content


def test_view_says_so_when_counting_is_disabled(staff_client, redis_client, settings):
    settings.SEARCH_TRAFFIC_COUNTING = False

    assert "SEARCH_TRAFFIC_COUNTING is off" in staff_client.get(TRAFFIC_URL).content.decode()


def test_view_renders_when_redis_is_down(staff_client, monkeypatch):
    def explode():
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(admin_views.traffic, "get_redis", explode)

    response = staff_client.get(TRAFFIC_URL)

    # The page you load when Redis is down has to report that rather than 500.
    assert response.status_code == 200
    assert "connection refused" in response.content.decode()
    assert "by_day" not in response.context


def test_admin_index_links_to_the_traffic_page(staff_client):
    assert TRAFFIC_URL in staff_client.get("/admin/").content.decode()
