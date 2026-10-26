"""Tests for volunteer tiers earned by crawling."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis
import pytest
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl import membership
from mwmbl.crawler.stats import StatsManager
from mwmbl.membership import MembershipTier, VolunteerTier, VolunteerTierInfo
from mwmbl.models import Membership, MwmblUser, VolunteerMembership
from mwmbl.volunteer_membership import end_of_next_month, user_combined_search_monthly_limit

TODAY = date(2026, 10, 17)
ENOUGH_RESULTS = 100
POLLINATOR = VolunteerTier.POLLINATOR


@pytest.fixture
def user(db):
    return MwmblUser.objects.create_user(username="crawler", email="crawler@example.com", password="testpass123")


@pytest.fixture
def today():
    with patch("mwmbl.volunteer_membership.utc_today", return_value=TODAY):
        yield TODAY


@pytest.fixture
def two_tiers(monkeypatch):
    """A second, higher volunteer tier, as if one had been added to VOLUNTEER_TIERS."""
    higher = VolunteerTierInfo(tier=POLLINATOR, days_per_month=5, min_results_per_day=ENOUGH_RESULTS, perks=[])
    lower = VolunteerTierInfo(tier="lower", days_per_month=3, min_results_per_day=ENOUGH_RESULTS, perks=[])
    tiers = [lower, higher]
    monkeypatch.setattr("mwmbl.volunteer_membership.VOLUNTEER_TIERS", tiers)
    monkeypatch.setattr("mwmbl.volunteer_membership.TIER_RANK", {"lower": 0, POLLINATOR: 1})
    return tiers


def _crawl(user, day: date, num_results: int = ENOUGH_RESULTS) -> None:
    results = SimpleNamespace(results=[object()] * num_results, crawler_version=None, device_name=None)
    with patch("mwmbl.crawler.stats.utc_today", return_value=day):
        StatsManager(fakeredis.FakeRedis()).record_results(results, user)


def _valid_until(user, tier=POLLINATOR) -> date | None:
    return VolunteerMembership.objects.filter(user=user, tier=tier).values_list("valid_until", flat=True).first()


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


def test_pollinator_is_earned_on_the_third_crawl_day(user):
    _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 5))
    assert _valid_until(user) is None

    _crawl(user, date(2026, 10, 9))

    assert _valid_until(user) == date(2026, 11, 30)


def test_several_batches_on_one_day_count_as_one_day(user):
    for _ in range(3):
        _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 2))

    assert _valid_until(user) is None


@pytest.mark.parametrize("num_results", [0, ENOUGH_RESULTS - 1])
def test_days_with_too_few_results_do_not_count(user, num_results):
    _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 2))
    _crawl(user, date(2026, 10, 3), num_results=num_results)

    assert _valid_until(user) is None


def test_a_day_counts_once_it_reaches_the_minimum(user):
    _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 2))
    _crawl(user, date(2026, 10, 3), num_results=ENOUGH_RESULTS - 1)
    _crawl(user, date(2026, 10, 3), num_results=1)

    assert _valid_until(user) == date(2026, 11, 30)


def test_days_in_earlier_months_do_not_count(user):
    _crawl(user, date(2026, 9, 29))
    _crawl(user, date(2026, 9, 30))
    _crawl(user, date(2026, 10, 1))

    assert _valid_until(user) is None


def test_earning_it_again_extends_the_membership(user):
    for day in (1, 2, 3):
        _crawl(user, date(2026, 10, day))
    for day in (1, 2, 3):
        _crawl(user, date(2026, 11, day))

    assert _valid_until(user) == date(2026, 12, 31)


def test_earning_it_does_not_shorten_a_longer_membership(user):
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=date(2027, 6, 30))
    for day in (1, 2, 3):
        _crawl(user, date(2026, 10, day))

    assert _valid_until(user) == date(2027, 6, 30)


def test_a_failed_grant_is_retried_on_the_next_batch(user):
    _crawl(user, date(2026, 10, 1))
    _crawl(user, date(2026, 10, 2))
    with patch("mwmbl.volunteer_membership.VolunteerMembership.objects.get_or_create", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            _crawl(user, date(2026, 10, 3))

    _crawl(user, date(2026, 10, 3))

    assert _valid_until(user) == date(2026, 11, 30)


def test_each_tier_is_earned_at_its_own_threshold(user, two_tiers):
    for day in (1, 2, 3):
        _crawl(user, date(2026, 10, day))
    assert _valid_until(user, "lower") == date(2026, 11, 30)
    assert _valid_until(user) is None

    for day in (4, 5):
        _crawl(user, date(2026, 10, day))

    assert _valid_until(user) == date(2026, 11, 30)


def test_every_volunteer_tier_has_a_combined_search_limit(settings):
    for info in membership.VOLUNTEER_TIERS:
        assert info.tier.value in settings.COMBINED_SEARCH_MONTHLY_LIMITS


def test_pollinator_sets_the_combined_search_limit(user, today):
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=today)
    assert user_combined_search_monthly_limit(user.id) == 300


def test_an_expired_volunteer_membership_gives_the_free_limit(user, today):
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=date(2026, 10, 16))
    assert user_combined_search_monthly_limit(user.id) == 30


@pytest.mark.parametrize(
    "paid_limit, pollinator_limit, expected",
    [(1_500, 300, 1_500), (300, 1_000, 1_000)],
)
def test_the_more_generous_limit_applies(user, today, settings, paid_limit, pollinator_limit, expected):
    settings.COMBINED_SEARCH_MONTHLY_LIMITS = {
        **settings.COMBINED_SEARCH_MONTHLY_LIMITS,
        "sprout": paid_limit,
        "pollinator": pollinator_limit,
    }
    Membership.objects.create(user=user, tier=MembershipTier.SPROUT, polar_subscription_id="sub_1")
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=today)

    assert user_combined_search_monthly_limit(user.id) == expected


def test_list_tiers():
    response = Client().get("/api/v2/membership/tiers")

    assert response.status_code == 200
    body = response.json()
    assert [tier["tier"] for tier in body["paid"]] == ["sprout", "sapling", "canopy"]
    assert body["volunteer"] == [
        {
            "tier": "pollinator",
            "name": "Pollinator",
            "days_per_month": 3,
            "min_results_per_day": 100,
            "perks": membership.VOLUNTEER_TIERS[0].perks,
        }
    ]


def _get_membership(user):
    with patch("mwmbl.platform.api.get_monthly_combined_search_count", return_value=12):
        return Client().get("/api/v2/membership/", **auth_headers(user))


def test_get_membership_for_a_non_member(user, today):
    response = _get_membership(user)

    assert response.status_code == 200
    assert response.json() == {
        "paid": None,
        "volunteer": None,
        "combined_search": {"monthly_usage": 12, "monthly_limit": 30},
    }


def test_get_membership_for_a_volunteer(user, today):
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=date(2026, 11, 30))

    response = _get_membership(user)

    assert response.json() == {
        "paid": None,
        "volunteer": {"tier": "pollinator", "name": "Pollinator", "valid_until": "2026-11-30"},
        "combined_search": {"monthly_usage": 12, "monthly_limit": 300},
    }


def test_get_membership_with_paid_and_volunteer_memberships(user, today):
    Membership.objects.create(
        user=user,
        tier=MembershipTier.SAPLING,
        polar_subscription_id="sub_1",
        current_period_end=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=date(2026, 11, 30))

    body = _get_membership(user).json()

    assert body["paid"] == {
        "tier": "sapling",
        "name": "Sapling",
        "current_period_end": "2026-11-03T00:00:00Z",
        "cancel_at_period_end": False,
    }
    assert body["volunteer"]["tier"] == "pollinator"
    assert body["combined_search"]["monthly_limit"] == 1_500


def test_get_membership_returns_the_highest_volunteer_tier(user, today, two_tiers):
    VolunteerMembership.objects.create(user=user, tier="lower", valid_until=date(2026, 12, 31))
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=date(2026, 11, 30))

    assert _get_membership(user).json()["volunteer"]["tier"] == "pollinator"


def test_get_membership_omits_an_expired_volunteer_membership(user, today):
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=date(2026, 10, 16))

    assert _get_membership(user).json()["volunteer"] is None


def test_get_membership_requires_login(db):
    assert Client().get("/api/v2/membership/").status_code == 401


def test_v1_membership_ignores_volunteer_memberships(user, today):
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=today)
    response = Client().get("/api/v1/platform/membership", **auth_headers(user))

    assert response.status_code == 404


def test_pollinator_sets_the_usage_limit(user, today):
    VolunteerMembership.objects.create(user=user, tier=POLLINATOR, valid_until=today)
    with patch("mwmbl.platform.api.get_monthly_combined_search_count", return_value=0):
        response = Client().get("/api/v1/platform/combined-search/usage", **auth_headers(user))

    assert response.json()["monthly_limit"] == 300
