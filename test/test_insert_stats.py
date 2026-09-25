"""Insert test stats data for testuser and fakeuser."""

from datetime import timedelta

import pytest

from mwmbl.models import MwmblUser, UserStats
from mwmbl.utils import utc_today


@pytest.mark.django_db
def test_insert_testuser_stats():
    """Insert test stats for testuser and fakeuser."""
    # Get or create testuser
    testuser, _ = MwmblUser.objects.get_or_create(username="testuser", defaults={"password": "testpass"})
    print(f"testuser: {testuser.id}")

    # Create a second fake user
    fakeuser, _ = MwmblUser.objects.get_or_create(username="fakeuser", defaults={"password": "testpass"})
    print(f"fakeuser: {fakeuser.id}")

    today = utc_today()
    for i in range(30):
        d = today - timedelta(days=i)
        # More results on recent days
        num_results = max(1, 50 - i) if i < 20 else max(1, 10 - (i - 20))
        UserStats.objects.update_or_create(user=testuser, date=d, defaults={"num_results": num_results})

    # Insert stats for fakeuser - different pattern
    for i in range(30):
        d = today - timedelta(days=i)
        # Steady but lower results
        num_results = max(1, 30 - i // 2)
        UserStats.objects.update_or_create(user=fakeuser, date=d, defaults={"num_results": num_results})

    print("Stats inserted!")
    testuser_total = sum(s.num_results for s in UserStats.objects.filter(user=testuser))
    fakeuser_total = sum(s.num_results for s in UserStats.objects.filter(user=fakeuser))
    print(f"testuser total: {testuser_total}")
    print(f"fakeuser total: {fakeuser_total}")

    # Verify
    assert testuser_total > 0
    assert fakeuser_total > 0
