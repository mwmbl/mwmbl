from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mwmbl.crawler.stats import StatsManager
from mwmbl.models import MwmblUser

TODAY = date(2026, 9, 22)


def _record(stats_manager: StatsManager, day: date, num_results: int, user: MwmblUser) -> None:
    results = SimpleNamespace(results=[object()] * num_results)
    with patch("mwmbl.crawler.stats.utc_today", return_value=day):
        stats_manager.record_results(results, user)


@pytest.mark.django_db
def test_user_results_count_is_kept_for_the_whole_stats_window():
    from mwmbl.models import MwmblUser

    stats_manager = StatsManager()

    # Create user in the database
    alice = MwmblUser.objects.create_user(username="alice", password="testpass")

    _record(stats_manager, TODAY, 5, alice)

    # Verify the data was persisted to UserStats table
    from mwmbl.models import UserStats

    stat = UserStats.objects.get(user=alice, date=TODAY)
    assert stat.num_results == 5


@pytest.mark.django_db
def test_user_stats_include_earlier_days():
    from mwmbl.models import MwmblUser

    stats_manager = StatsManager()
    ten_days_ago = TODAY - timedelta(days=10)

    # Create users in the database
    alice = MwmblUser.objects.create_user(username="alice", password="testpass")
    bob = MwmblUser.objects.create_user(username="bob", password="testpass")

    _record(stats_manager, ten_days_ago, 3, alice)
    _record(stats_manager, TODAY, 5, alice)
    _record(stats_manager, TODAY, 7, bob)

    with patch("mwmbl.crawler.stats.utc_today", return_value=TODAY):
        user_stats = stats_manager.get_user_stats("alice")

    daily = user_stats["results_indexed_daily"]
    assert len(daily) == 30
    assert daily[str(ten_days_ago)] == 3
    assert daily[str(TODAY)] == 5
    assert user_stats["results_indexed_today"] == 5
