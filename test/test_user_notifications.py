"""Tests for notifying volunteers of membership grants, and for listing and dismissing notifications."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis
import pytest
from django.contrib.admin.sites import AdminSite
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl.admin import VolunteerMembershipAdmin
from mwmbl.crawler.stats import StatsManager
from mwmbl.membership import VolunteerTier
from mwmbl.models import MwmblUser, NotificationKind, UserNotification, VolunteerMembership

TODAY = date(2026, 10, 17)
POLLINATOR = VolunteerTier.POLLINATOR
GRANTED = NotificationKind.VOLUNTEER_MEMBERSHIP_GRANTED


@pytest.fixture
def user(db):
    return MwmblUser.objects.create_user(username="crawler", email="crawler@example.com", password="testpass123")


def _crawl(user, day: date) -> None:
    results = SimpleNamespace(results=[object()] * 100)
    with patch("mwmbl.crawler.stats.utc_today", return_value=day):
        StatsManager(fakeredis.FakeRedis()).record_results(results, user)


def _crawl_three_days(user, year: int, month: int) -> None:
    for day in (1, 2, 3):
        _crawl(user, date(year, month, day))


def _notification_data(user) -> list[dict]:
    return list(UserNotification.objects.filter(user=user, kind=GRANTED).order_by("id").values_list("data", flat=True))


def auth_headers(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def test_earning_a_tier_notifies_the_user(user):
    _crawl_three_days(user, 2026, 10)

    assert _notification_data(user) == [{"tier": "pollinator", "valid_until": "2026-11-30"}]


def test_crawling_after_earning_a_tier_does_not_notify_again(user):
    _crawl_three_days(user, 2026, 10)
    _crawl(user, date(2026, 10, 4))

    assert len(_notification_data(user)) == 1


def test_extending_a_current_membership_does_not_notify(user):
    _crawl_three_days(user, 2026, 10)
    _crawl_three_days(user, 2026, 11)

    assert len(_notification_data(user)) == 1


def test_earning_a_lapsed_membership_again_notifies(user):
    _crawl_three_days(user, 2026, 10)
    _crawl_three_days(user, 2027, 1)

    assert _notification_data(user)[1] == {"tier": "pollinator", "valid_until": "2027-02-28"}


def _save_in_admin(volunteer_membership, change: bool, initial_valid_until: date | None = None) -> None:
    form = SimpleNamespace(initial={"valid_until": initial_valid_until} if change else {})
    with patch("mwmbl.admin.utc_today", return_value=TODAY):
        VolunteerMembershipAdmin(VolunteerMembership, AdminSite()).save_model(None, volunteer_membership, form, change)


def test_a_membership_gifted_in_the_admin_notifies(user):
    _save_in_admin(VolunteerMembership(user=user, tier=POLLINATOR, valid_until=date(2027, 6, 30)), change=False)

    assert _notification_data(user) == [{"tier": "pollinator", "valid_until": "2027-06-30"}]


@pytest.mark.parametrize(
    "initial_valid_until, valid_until, notified",
    [
        (date(2026, 9, 30), date(2027, 6, 30), True),
        (date(2026, 11, 30), date(2027, 6, 30), False),
        (date(2026, 9, 30), date(2026, 10, 1), False),
    ],
    ids=["reviving a lapsed one", "extending a current one", "editing a lapsed one that stays lapsed"],
)
def test_editing_a_membership_in_the_admin(user, initial_valid_until, valid_until, notified):
    volunteer_membership = VolunteerMembership.objects.create(
        user=user, tier=POLLINATOR, valid_until=initial_valid_until
    )
    volunteer_membership.valid_until = valid_until

    _save_in_admin(volunteer_membership, change=True, initial_valid_until=initial_valid_until)

    assert bool(_notification_data(user)) == notified


def test_list_notifications(user):
    _crawl_three_days(user, 2026, 10)

    response = Client().get("/api/v2/notifications/", **auth_headers(user))

    assert response.status_code == 200
    [notification] = response.json()
    assert notification["kind"] == "volunteer_membership_granted"
    assert notification["data"] == {"tier": "pollinator", "valid_until": "2026-11-30"}


def test_dismissed_notifications_are_not_listed(user):
    _crawl_three_days(user, 2026, 10)
    notification = UserNotification.objects.get(user=user)

    response = Client().post(f"/api/v2/notifications/{notification.id}/dismiss", **auth_headers(user))

    assert response.status_code == 204
    notification.refresh_from_db()
    assert notification.dismissed_at is not None
    assert Client().get("/api/v2/notifications/", **auth_headers(user)).json() == []


def test_dismissing_twice_keeps_the_first_dismissal(user):
    _crawl_three_days(user, 2026, 10)
    notification = UserNotification.objects.get(user=user)
    url = f"/api/v2/notifications/{notification.id}/dismiss"
    Client().post(url, **auth_headers(user))
    notification.refresh_from_db()
    first_dismissed_at = notification.dismissed_at

    response = Client().post(url, **auth_headers(user))

    assert response.status_code == 204
    notification.refresh_from_db()
    assert notification.dismissed_at == first_dismissed_at


def test_users_cannot_see_or_dismiss_each_others_notifications(user):
    _crawl_three_days(user, 2026, 10)
    notification = UserNotification.objects.get(user=user)
    other = MwmblUser.objects.create_user(username="other", email="other@example.com", password="testpass123")

    assert Client().get("/api/v2/notifications/", **auth_headers(other)).json() == []
    response = Client().post(f"/api/v2/notifications/{notification.id}/dismiss", **auth_headers(other))

    assert response.status_code == 404
    notification.refresh_from_db()
    assert notification.dismissed_at is None


def test_notifications_require_login(db):
    assert Client().get("/api/v2/notifications/").status_code == 401
    assert Client().post("/api/v2/notifications/1/dismiss").status_code == 401
