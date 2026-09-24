from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis
import pytest

from mwmbl.crawler.stats import LONG_EXPIRE_SECONDS, USER_RESULTS_COUNT_KEY, StatsManager

TODAY = date(2026, 9, 22)


def _record(stats_manager: StatsManager, day: date, num_results: int, username: str) -> None:
    results = SimpleNamespace(results=[object()] * num_results)
    with patch("mwmbl.crawler.stats.utc_today", return_value=day):
        stats_manager.record_results(results, username)


@pytest.mark.django_db
def test_user_results_count_is_kept_for_the_whole_stats_window():
    from mwmbl.models import MwmblUser

    redis = fakeredis.FakeRedis()
    stats_manager = StatsManager(redis)

    # Create user in the database
    MwmblUser.objects.create_user(username="alice", password="testpass")

    _record(stats_manager, TODAY, 5, "alice")

    user_results_count_key = USER_RESULTS_COUNT_KEY.format(date=TODAY)
    assert redis.ttl(user_results_count_key) == LONG_EXPIRE_SECONDS


@pytest.mark.django_db
def test_user_stats_include_earlier_days():
    from mwmbl.models import MwmblUser

    stats_manager = StatsManager(fakeredis.FakeRedis())
    ten_days_ago = TODAY - timedelta(days=10)

    # Create user in the database
    MwmblUser.objects.create_user(username="alice", password="testpass")

    _record(stats_manager, ten_days_ago, 3, "alice")
    _record(stats_manager, TODAY, 5, "alice")
    _record(stats_manager, TODAY, 7, "bob")

    with patch("mwmbl.crawler.stats.utc_today", return_value=TODAY):
        user_stats = stats_manager.get_user_stats("alice")

    daily = user_stats["results_indexed_daily"]
    assert len(daily) == 30
    assert daily[str(ten_days_ago)] == 3
    assert daily[str(TODAY)] == 5
    assert user_stats["results_indexed_today"] == 5
