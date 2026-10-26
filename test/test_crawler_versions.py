"""Tests for the crawler version spread: recorded per submission, served to staff by the platform API."""

import re
from datetime import date, timedelta
from unittest.mock import patch

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from ninja_jwt.tokens import RefreshToken

from mwmbl.crawler.batch import MAX_ERROR_TAG_LENGTH, Result, Results
from mwmbl.crawler.stats import (
    CRAWLER_VERSION_DEVICES_KEY,
    CRAWLER_VERSION_RESULTS_KEY,
    LONG_EXPIRE_SECONDS,
    StatsManager,
)
from mwmbl.crawler.version import CRAWLER_VERSION
from mwmbl.models import ApiKey, generate_api_key
from mwmbl.utils import utc_today

User = get_user_model()

VERSIONS_URL = "/api/v1/platform/crawler-versions"
TODAY = date(2026, 10, 26)


def _submit(stats_manager, user, day, version, device_name, num_results=1):
    results = Results(
        results=[Result(url=f"https://example.com/{i}", title="t", extract="e") for i in range(num_results)],
        crawler_version=version,
        device_name=device_name,
    )
    with patch("mwmbl.crawler.stats.utc_today", return_value=day):
        stats_manager.record_results(results, user)


def _spread(stats_manager, days=7):
    with patch("mwmbl.crawler.stats.utc_today", return_value=TODAY):
        return stats_manager.get_crawler_version_spread(days)


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {RefreshToken.for_user(user).access_token}"}


def test_crawler_version_is_what_the_release_tags_match():
    # .github/workflows/crawler-release.yml only publishes crawler-vX.Y.Z tags.
    assert re.fullmatch(r"\d+\.\d+\.\d+", CRAWLER_VERSION)


@pytest.fixture
def stats_manager():
    return StatsManager(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="testpass")


@pytest.fixture
def bob(db):
    return User.objects.create_user(username="bob", password="testpass")


def test_crawlers_sharing_a_user_count_separately_by_device(stats_manager, alice, bob):
    _submit(stats_manager, alice, TODAY, "0.2.0", "crawler-aaaa", num_results=3)
    _submit(stats_manager, alice, TODAY, "0.2.0", "crawler-aaaa", num_results=2)
    _submit(stats_manager, alice, TODAY, "0.2.0", "crawler-bbbb")
    # The same device name under another user is another crawler.
    _submit(stats_manager, bob, TODAY, "0.2.0", "crawler-aaaa")

    spread = _spread(stats_manager)

    [row] = spread.rows
    assert row.version == "0.2.0"
    assert row.days[-1].crawlers == 3
    assert row.days[-1].results == 7
    assert spread.total_crawlers[-1] == 3


def test_versions_sort_newest_first_with_unknown_last(stats_manager, alice):
    _submit(stats_manager, alice, TODAY, "0.9.0", "a")
    _submit(stats_manager, alice, TODAY, None, None)
    _submit(stats_manager, alice, TODAY, "0.10.0", "b")

    spread = _spread(stats_manager)

    assert [row.version for row in spread.rows] == ["0.10.0", "0.9.0", "unknown"]


def test_a_crawler_updated_during_a_day_counts_once_in_the_total(stats_manager, alice):
    _submit(stats_manager, alice, TODAY, "0.2.0", "crawler-aaaa")
    _submit(stats_manager, alice, TODAY, "0.3.0", "crawler-aaaa")

    spread = _spread(stats_manager)

    assert [row.days[-1].crawlers for row in spread.rows] == [1, 1]
    assert spread.total_crawlers[-1] == 1


def test_spread_covers_each_of_the_last_days_oldest_first(stats_manager, alice):
    _submit(stats_manager, alice, TODAY - timedelta(days=6), "0.2.0", "a")
    _submit(stats_manager, alice, TODAY - timedelta(days=7), "0.1.0", "a")

    spread = _spread(stats_manager)

    assert spread.dates == [TODAY - timedelta(days=offset) for offset in range(6, -1, -1)]
    [row] = spread.rows
    assert row.version == "0.2.0"
    assert [day.crawlers for day in row.days] == [1, 0, 0, 0, 0, 0, 0]
    assert spread.total_crawlers == [1, 0, 0, 0, 0, 0, 0]


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="staff_member_1", password="correctpassword", is_staff=True)


def test_anonymous_users_are_refused(client, db):
    response = client.get(VERSIONS_URL)

    assert response.status_code == 401


def test_non_staff_are_refused(client, alice):
    response = client.get(VERSIONS_URL, headers=_auth(alice))

    assert response.status_code == 403


def test_staff_get_the_spread(client, staff, alice, stats_manager):
    _submit(stats_manager, alice, utc_today(), "0.2.0", "crawler-aaaa", num_results=4)

    with patch("mwmbl.platform.api.stats_manager", stats_manager):
        response = client.get(VERSIONS_URL, headers=_auth(staff))

    assert response.status_code == 200
    body = response.json()
    assert len(body["dates"]) == 7
    assert body["dates"][-1] == str(utc_today())
    assert body["rows"] == [
        {"version": "0.2.0", "days": [{"crawlers": 0, "results": 0}] * 6 + [{"crawlers": 1, "results": 4}]}
    ]
    assert body["total_crawlers"] == [0] * 6 + [1]


def test_a_version_longer_than_an_error_tag_is_cut_to_that_length(stats_manager, alice):
    _submit(stats_manager, alice, TODAY, "1." * 500, "a")

    spread = _spread(stats_manager)

    [row] = spread.rows
    assert len(row.version) == MAX_ERROR_TAG_LENGTH


def test_a_version_that_is_not_all_numbers_does_not_break_the_ordering(stats_manager, alice):
    _submit(stats_manager, alice, TODAY, "0.2.0", "a")
    _submit(stats_manager, alice, TODAY, "0.2.0-rc1", "b")
    _submit(stats_manager, alice, TODAY, "0.10.0", "c")

    spread = _spread(stats_manager)

    assert spread.rows[0].version == "0.10.0"
    assert {row.version for row in spread.rows} == {"0.10.0", "0.2.0", "0.2.0-rc1"}


def test_the_counters_expire_with_the_other_stats(stats_manager, alice):
    _submit(stats_manager, alice, TODAY, "0.2.0", "a")

    results_key = CRAWLER_VERSION_RESULTS_KEY.format(date=TODAY)
    devices_key = CRAWLER_VERSION_DEVICES_KEY.format(date=TODAY, version="0.2.0")
    assert stats_manager.redis.ttl(results_key) == LONG_EXPIRE_SECONDS
    assert stats_manager.redis.ttl(devices_key) == LONG_EXPIRE_SECONDS


@pytest.fixture
def crawl_key(alice):
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=alice, key=key_hash, name="Crawl key", scopes=[ApiKey.Scope.CRAWL])
    return raw_key


def _post_results(client, stats_manager, crawl_key, path="/api/v1/crawler/results"):
    with (
        patch("mwmbl.crawler.app.index_documents"),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager", stats_manager),
        patch("mwmbl.crawler.app.record_agreements"),
        patch("mwmbl.crawler.worst_agreements.record_worst_agreements"),
    ):
        return client.post(
            path,
            content_type="application/json",
            data={
                "results": [{"url": "https://example.com/a", "title": "t", "extract": "e"}],
                "crawler_version": "0.2.0",
                "device_name": "crawler-aaaa",
            },
            HTTP_X_API_KEY=crawl_key,
        )


def test_the_results_endpoint_counts_the_crawler_under_its_version(client, stats_manager, crawl_key):
    response = _post_results(client, stats_manager, crawl_key)

    assert response.status_code == 200
    spread = stats_manager.get_crawler_version_spread(7)
    [row] = spread.rows
    assert row.version == "0.2.0"
    assert (row.days[-1].crawlers, row.days[-1].results) == (1, 1)


def test_a_dry_run_is_not_counted(client, stats_manager, crawl_key):
    response = _post_results(client, stats_manager, crawl_key, "/api/v1/crawler/results?dry_run=true")

    assert response.status_code == 200
    assert stats_manager.get_crawler_version_spread(7).rows == []
