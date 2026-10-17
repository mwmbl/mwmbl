"""Tests for free Sprout memberships earned by crawling."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis
import pytest
from allauth.account.models import EmailAddress
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl.crawler.stats import StatsManager
from mwmbl.crawler_membership import end_of_next_month, membership_tier
from mwmbl.membership import MembershipTier
from mwmbl.models import CrawlerMembership, Membership, MwmblUser

TODAY = date(2026, 10, 17)
ENOUGH_RESULTS = 100


@pytest.fixture
def user(db):
    user = MwmblUser.objects.create_user(username="crawler", email="crawler@example.com", password="testpass123")
    EmailAddress.objects.create(user=user, email="crawler@example.com", verified=True, primary=True)
    return user


@pytest.fixture
def today():
    with (
        patch("mwmbl.crawler_membership.utc_today", return_value=TODAY),
        patch("mwmbl.platform.api.utc_today", return_value=TODAY),
    ):
        yield TODAY


def _crawl(user, day: date, num_results: int = ENOUGH_RESULTS) -> None:
    results = SimpleNamespace(results=[object()] * num_results)
    with patch("mwmbl.crawler.stats.utc_today", return_value=day):
        StatsManager(fakeredis.FakeRedis()).record_results(results, user)


def auth_headers(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


@pytest.mark.parametrize(
    "day, expected",
    [
        (date(2026, 10, 17), date(2026, 11, 30)),
        (date(2026, 12, 1), date(2027, 1, 31)),
        (date(2028, 1, 31), date(2028, 2, 29)),
    ],
)
def test_end_of_next_month(day, expected):
    assert end_of_next_month(day) == expected


def test_membership_is_granted_on_the_third_crawl_day(user):
    _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 5))
    assert not CrawlerMembership.objects.filter(user=user).exists()

    _crawl(user, date(2026, 10, 9))

    assert CrawlerMembership.objects.get(user=user).valid_until == date(2026, 11, 30)


def test_several_batches_on_one_day_count_as_one_day(user):
    for _ in range(3):
        _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 2))

    assert not CrawlerMembership.objects.filter(user=user).exists()


@pytest.mark.parametrize("num_results", [0, ENOUGH_RESULTS - 1])
def test_days_with_too_few_results_do_not_count(user, num_results):
    _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 2))
    _crawl(user, date(2026, 10, 3), num_results=num_results)

    assert not CrawlerMembership.objects.filter(user=user).exists()


def test_membership_is_granted_when_a_day_reaches_the_minimum(user):
    _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 2))
    _crawl(user, date(2026, 10, 3), num_results=ENOUGH_RESULTS - 1)
    _crawl(user, date(2026, 10, 3), num_results=1)

    assert CrawlerMembership.objects.get(user=user).valid_until == date(2026, 11, 30)


def test_days_in_earlier_months_do_not_count(user):
    _crawl(user, date(2026, 9, 29))
    _crawl(user, date(2026, 9, 30))
    _crawl(user, date(2026, 10, 1))

    assert not CrawlerMembership.objects.filter(user=user).exists()


def test_qualifying_again_extends_the_membership(user):
    for day in (1, 2, 3):
        _crawl(user, date(2026, 10, day))
    for day in (1, 2, 3):
        _crawl(user, date(2026, 11, day))

    assert CrawlerMembership.objects.get(user=user).valid_until == date(2026, 12, 31)


def test_qualifying_does_not_shorten_a_longer_membership(user):
    CrawlerMembership.objects.create(user=user, valid_until=date(2027, 6, 30))
    for day in (1, 2, 3):
        _crawl(user, date(2026, 10, day))

    assert CrawlerMembership.objects.get(user=user).valid_until == date(2027, 6, 30)


def test_a_failed_grant_is_retried_on_the_next_batch(user):
    _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 2))
    with patch("mwmbl.crawler_membership.CrawlerMembership.objects.get_or_create", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            _crawl(user, date(2026, 10, 3))

    _crawl(user, date(2026, 10, 3))

    assert CrawlerMembership.objects.get(user=user).valid_until == date(2026, 11, 30)


def test_crawler_membership_gives_sprout(user, today):
    CrawlerMembership.objects.create(user=user, valid_until=today)
    assert membership_tier(user.id) == MembershipTier.SPROUT


def test_expired_crawler_membership_gives_nothing(user, today):
    CrawlerMembership.objects.create(user=user, valid_until=date(2026, 10, 16))
    assert membership_tier(user.id) is None


def test_paid_membership_wins_over_crawler_membership(user, today):
    CrawlerMembership.objects.create(user=user, valid_until=today)
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_1")
    assert membership_tier(user.id) == MembershipTier.SAPLING


def test_get_membership_returns_crawler_membership(user, today):
    CrawlerMembership.objects.create(user=user, valid_until=date(2026, 11, 30))
    response = Client().get("/api/v1/platform/membership", **auth_headers(user))

    assert response.status_code == 200
    assert response.json() == {
        "tier": "sprout",
        "current_period_end": datetime(2026, 12, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "cancel_at_period_end": False,
        "source": "crawler",
    }


def test_get_membership_is_404_when_the_crawler_membership_has_expired(user, today):
    CrawlerMembership.objects.create(user=user, valid_until=date(2026, 10, 16))
    response = Client().get("/api/v1/platform/membership", **auth_headers(user))

    assert response.status_code == 404


def test_get_membership_marks_paid_membership(user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_1")
    response = Client().get("/api/v1/platform/membership", **auth_headers(user))

    assert response.status_code == 200
    assert response.json()["source"] == "paid"


def test_get_membership_prefers_the_paid_membership(user, today):
    CrawlerMembership.objects.create(user=user, valid_until=today)
    Membership.objects.create(user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_1")
    response = Client().get("/api/v1/platform/membership", **auth_headers(user))

    assert response.json()["tier"] == "canopy"
    assert response.json()["source"] == "paid"


@pytest.mark.parametrize(
    "path, body",
    [("cancel", None), ("uncancel", None), ("change", {"tier": "sapling"})],
)
def test_crawler_memberships_cannot_be_cancelled_or_changed(user, today, path, body):
    CrawlerMembership.objects.create(user=user, valid_until=today)
    response = Client().post(
        f"/api/v1/platform/membership/{path}", body, content_type="application/json", **auth_headers(user)
    )

    assert response.status_code == 409


def test_crawler_membership_sets_the_combined_search_limit(user, today):
    CrawlerMembership.objects.create(user=user, valid_until=today)
    with patch("mwmbl.platform.api.get_monthly_combined_search_count", return_value=0):
        response = Client().get("/api/v1/platform/combined-search/usage", **auth_headers(user))

    assert response.json()["monthly_limit"] == 300
