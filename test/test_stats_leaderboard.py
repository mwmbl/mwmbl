"""
Tests for the cached leaderboards.
"""

from datetime import timedelta
from unittest.mock import patch

import fakeredis
import pytest

from mwmbl.crawler.stats import (
    ALL_TIME_LEADERBOARD_CACHE_SECONDS,
    LEADERBOARD_CACHE_KEY,
    StatsManager,
)
from mwmbl.models import MwmblUser, UserStats
from mwmbl.utils import utc_today


def _user(username: str, seed_searches: int, seed_pages: int) -> MwmblUser:
    user = MwmblUser.objects.create_user(username=username, password="x")
    user.seed_searches = seed_searches
    user.seed_search_pages_indexed = seed_pages
    user.save()
    return user


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

    ttl = redis.ttl(LEADERBOARD_CACHE_KEY.format(period="all", date=utc_today()))
    assert 0 < ttl <= ALL_TIME_LEADERBOARD_CACHE_SECONDS
    assert ALL_TIME_LEADERBOARD_CACHE_SECONDS == 24 * 60 * 60


@pytest.mark.django_db
def test_payloads_cached_before_midnight_are_not_served_after_it():
    redis = fakeredis.FakeRedis(decode_responses=True)
    stats_manager = StatsManager(redis)
    today = utc_today()
    alice = _user("alice", 0, 0)
    UserStats.objects.create(user=alice, date=today, num_results=10)
    UserStats.objects.create(user=alice, date=today - timedelta(days=1), num_results=3)

    with patch("mwmbl.crawler.stats.utc_today", return_value=today):
        before = stats_manager.get_cached_leaderboard("yesterday")
    with patch("mwmbl.crawler.stats.utc_today", return_value=today + timedelta(days=1)):
        after = stats_manager.get_cached_leaderboard("yesterday")

    assert before == [("alice", 3)]
    assert after == [("alice", 10)]


@pytest.mark.django_db
def test_an_unknown_leaderboard_period_fails():
    stats_manager = StatsManager(fakeredis.FakeRedis(decode_responses=True))

    with pytest.raises(ValueError):
        stats_manager.get_cached_leaderboard("last-week")
