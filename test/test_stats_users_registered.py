"""
Tests for the weekly count of registered users shown on the stats endpoint.
"""

import json
from datetime import datetime, time, timedelta, timezone

import fakeredis
import pytest

from mwmbl.crawler.stats import (
    REGISTRATION_WEEKS,
    USERS_REGISTERED_WEEKLY_EXPIRE_SECONDS,
    USERS_REGISTERED_WEEKLY_KEY,
    StatsManager,
)
from mwmbl.models import MwmblUser
from mwmbl.utils import utc_today


def _this_week_start():
    today = utc_today()
    return today - timedelta(days=today.weekday())


def _register(username: str, joined: datetime) -> None:
    user = MwmblUser.objects.create_user(username=username, password="testpass")
    user.date_joined = joined
    user.save()


def _at_noon(day) -> datetime:
    return datetime.combine(day, time(12), tzinfo=timezone.utc)


@pytest.mark.django_db
def test_registrations_are_counted_in_the_week_they_happened():
    this_week_start = _this_week_start()
    last_week_start = this_week_start - timedelta(weeks=1)
    _register("alice", _at_noon(this_week_start))
    _register("bob", _at_noon(this_week_start + timedelta(days=6)))
    _register("carol", _at_noon(last_week_start + timedelta(days=3)))

    weekly = StatsManager(fakeredis.FakeRedis(decode_responses=True)).get_users_registered_weekly()

    assert len(weekly) == REGISTRATION_WEEKS
    assert list(weekly)[-1] == str(this_week_start)
    assert weekly[str(this_week_start)] == 2
    assert weekly[str(last_week_start)] == 1
    assert sum(weekly.values()) == 3


@pytest.mark.django_db
def test_registrations_older_than_a_year_are_left_out():
    first_week_start = _this_week_start() - timedelta(weeks=REGISTRATION_WEEKS - 1)
    _register("old", _at_noon(first_week_start - timedelta(days=1)))

    weekly = StatsManager(fakeredis.FakeRedis(decode_responses=True)).get_users_registered_weekly()

    assert set(weekly.values()) == {0}


@pytest.mark.django_db
def test_weekly_registrations_are_cached_in_redis():
    redis = fakeredis.FakeRedis(decode_responses=True)
    stats_manager = StatsManager(redis)
    _register("alice", _at_noon(_this_week_start()))

    first = stats_manager.get_users_registered_weekly()
    _register("bob", _at_noon(_this_week_start()))
    second = stats_manager.get_users_registered_weekly()

    assert second == first
    assert json.loads(redis.get(USERS_REGISTERED_WEEKLY_KEY)) == first
    assert 0 < redis.ttl(USERS_REGISTERED_WEEKLY_KEY) <= USERS_REGISTERED_WEEKLY_EXPIRE_SECONDS
