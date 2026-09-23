from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis

from mwmbl.crawler.stats import LONG_EXPIRE_SECONDS, USER_RESULTS_COUNT_KEY, StatsManager

TODAY = date(2026, 9, 22)


def _record(stats_manager: StatsManager, day: date, num_results: int, username: str) -> None:
    results = SimpleNamespace(results=[object()] * num_results)
    with patch("mwmbl.crawler.stats.utc_today", return_value=day):
        stats_manager.record_results(results, username)


def test_user_results_count_is_kept_for_the_whole_stats_window():
    redis = fakeredis.FakeRedis()
    stats_manager = StatsManager(redis)

    _record(stats_manager, TODAY, 5, "alice")

    user_results_count_key = USER_RESULTS_COUNT_KEY.format(date=TODAY)
    assert redis.ttl(user_results_count_key) == LONG_EXPIRE_SECONDS


def test_user_stats_include_earlier_days():
    stats_manager = StatsManager(fakeredis.FakeRedis())
    ten_days_ago = TODAY - timedelta(days=10)

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
