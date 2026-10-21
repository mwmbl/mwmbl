"""
Tests for the public stats cache and the Seed Search totals on the stats endpoint.
"""

from unittest.mock import patch

import fakeredis
import pytest

from mwmbl.crawler.stats import (
    ALL_TIME_LEADERBOARD_CACHE_SECONDS,
    LEADERBOARD_CACHE_KEY,
    STATS_CACHE_KEY,
    STATS_CACHE_SECONDS,
    StatsManager,
)
from mwmbl.models import MwmblUser, SeedDomain, UserStats
from mwmbl.utils import utc_today

NO_INDEX_COUNTS = {
    "urls_in_index_daily": {},
    "domains_in_index_daily": {},
    "results_in_index_daily": {},
}


def _user(username: str, seed_searches: int, seed_pages: int) -> MwmblUser:
    user = MwmblUser.objects.create_user(username=username, password="x")
    user.seed_searches = seed_searches
    user.seed_search_pages_indexed = seed_pages
    user.save()
    return user


@pytest.mark.django_db
def test_seed_totals_sum_every_users_activity():
    _user("alice", 3, 100)
    _user("bob", 4, 20)
    SeedDomain.objects.create(domain="a.example")
    SeedDomain.objects.create(domain="b.example")
    stats_manager = StatsManager(fakeredis.FakeRedis(decode_responses=True))

    with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS):
        stats = stats_manager.get_stats()

    assert stats.seed_searches_total == 7
    assert stats.seed_pages_indexed_total == 120
    assert stats.seed_sites_discovered_total == 2


@pytest.mark.django_db
def test_stats_are_built_once_then_served_from_redis():
    redis = fakeredis.FakeRedis(decode_responses=True)
    stats_manager = StatsManager(redis)
    _user("alice", 1, 1)

    with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS) as get_counts:
        first = stats_manager.get_stats()
        _user("bob", 5, 5)
        second = stats_manager.get_stats()

    assert get_counts.call_count == 1
    assert second == first
    assert 0 < redis.ttl(STATS_CACHE_KEY) <= STATS_CACHE_SECONDS


@pytest.mark.django_db
def test_leaderboards_are_cached():
    redis = fakeredis.FakeRedis(decode_responses=True)
    stats_manager = StatsManager(redis)
    alice = _user("alice", 0, 0)
    UserStats.objects.create(user=alice, date=utc_today(), num_results=10)

    first = stats_manager.get_cached_leaderboard("all")
    UserStats.objects.create(user=_user("bob", 0, 0), date=utc_today(), num_results=99)
    second = stats_manager.get_cached_leaderboard("all")

    assert first == [("alice", 10)]
    assert second == first


@pytest.mark.django_db
def test_the_all_time_leaderboard_is_cached_for_a_day():
    redis = fakeredis.FakeRedis(decode_responses=True)
    StatsManager(redis).get_cached_leaderboard("all")

    ttl = redis.ttl(LEADERBOARD_CACHE_KEY.format(period="all"))
    assert STATS_CACHE_SECONDS < ttl <= ALL_TIME_LEADERBOARD_CACHE_SECONDS
    assert ALL_TIME_LEADERBOARD_CACHE_SECONDS == 24 * 60 * 60


@pytest.mark.django_db
def test_an_unknown_leaderboard_period_fails():
    stats_manager = StatsManager(fakeredis.FakeRedis(decode_responses=True))

    with pytest.raises(ValueError):
        stats_manager.get_cached_leaderboard("last-week")
