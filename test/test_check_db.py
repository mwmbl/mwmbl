"""Check what's in the database."""

import pytest

from mwmbl.models import UserStats


@pytest.mark.django_db
def test_check_db():
    """Check what's in the database."""
    stats = UserStats.objects.all().order_by("date")
    for s in stats:
        print(f"{s.user.username}: {s.date} = {s.num_results}")

    # Also check totals
    from django.db.models import Sum

    totals = UserStats.objects.values("user__username").annotate(total=Sum("num_results"))
    for t in totals:
        print(f"Total for {t['user__username']}: {t['total']}")
