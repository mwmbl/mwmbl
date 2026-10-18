"""Test the leaderboard functions with inserted data."""

from datetime import timedelta

import fakeredis
import pytest

from mwmbl.crawler.stats import StatsManager
from mwmbl.models import MwmblUser, UserStats


@pytest.mark.django_db
def test_leaderboard_functions():
    """Test the leaderboard functions with inserted data."""
    from mwmbl.utils import utc_today

    # First, insert test data
    testuser, _ = MwmblUser.objects.get_or_create(username="testuser", defaults={"password": "testpass"})
    fakeuser, _ = MwmblUser.objects.get_or_create(username="fakeuser", defaults={"password": "testpass"})

    today = utc_today()

    # Insert stats for testuser - last 30 days with varying amounts
    for i in range(30):
        d = today - timedelta(days=i)
        num_results = max(1, 50 - i) if i < 20 else max(1, 10 - (i - 20))
        UserStats.objects.update_or_create(user=testuser, date=d, defaults={"num_results": num_results})

    # Insert stats for fakeuser - different pattern
    for i in range(30):
        d = today - timedelta(days=i)
        num_results = max(1, 30 - i // 2)
        UserStats.objects.update_or_create(user=fakeuser, date=d, defaults={"num_results": num_results})

    print("Test data inserted!")

    redis = fakeredis.FakeRedis()
    stats_manager = StatsManager(redis)

    # Test get_all_time_leaderboard
    all_time = stats_manager.get_all_time_leaderboard()
    print(f"All-time leaderboard: {all_time}")

    # Test get_leaderboard_for_date (today)
    today_leaderboard = stats_manager.get_leaderboard_for_date(today)
    print(f"Today leaderboard: {today_leaderboard}")

    # Test get_leaderboard_for_date (yesterday)
    yesterday = today - timedelta(days=1)
    yesterday_leaderboard = stats_manager.get_leaderboard_for_date(yesterday)
    print(f"Yesterday leaderboard: {yesterday_leaderboard}")

    # Test get_user_stats for testuser
    testuser_stats = stats_manager.get_user_stats("testuser")
    print(f"testuser stats: {testuser_stats}")

    # Test get_user_stats for fakeuser
    fakeuser_stats = stats_manager.get_user_stats("fakeuser")
    print(f"fakeuser stats: {fakeuser_stats}")

    # Verify testuser has more total results than fakeuser
    assert len(all_time) >= 2
    assert all_time[0][0] == "testuser"  # testuser should be first
    assert all_time[1][0] == "fakeuser"  # fakeuser should be second

    # Verify today's leaderboard
    assert len(today_leaderboard) >= 2
    assert today_leaderboard[0][0] == "testuser"
    assert today_leaderboard[1][0] == "fakeuser"

    # Verify user stats
    assert testuser_stats["results_indexed_today"] > fakeuser_stats["results_indexed_today"]
    assert sum(testuser_stats["results_indexed_daily"].values()) > sum(fakeuser_stats["results_indexed_daily"].values())
