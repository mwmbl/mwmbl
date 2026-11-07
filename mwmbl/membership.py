"""Membership tiers: supporter subscriptions sold through Polar, separate from metered API billing."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import models


class MembershipTier(models.TextChoices):
    SPROUT = "sprout", "Sprout"
    SAPLING = "sapling", "Sapling"
    CANOPY = "canopy", "Canopy"


@dataclass(frozen=True)
class TierInfo:
    tier: MembershipTier
    monthly_price_pence: int
    perks: list[str]


TIERS = [
    TierInfo(
        tier=MembershipTier.SPROUT,
        monthly_price_pence=100,
        perks=[
            "Access to the members area in Matrix and Discord",
            "300 Seed Search queries a month to enhance our index",
        ],
    ),
    TierInfo(
        tier=MembershipTier.SAPLING,
        monthly_price_pence=500,
        perks=[
            "Everything in Sprout",
            "1,500 Seed Search queries a month to enhance our index",
        ],
    ),
    TierInfo(
        tier=MembershipTier.CANOPY,
        monthly_price_pence=2_000,
        perks=[
            "Everything in Sapling",
            "1 million pages a month crawled against your username",
            "Your username on the crawler leaderboard",
        ],
    ),
]


class VolunteerTier(models.TextChoices):
    """Tiers earned by volunteering rather than bought. Kept apart from MembershipTier, which is sold through Polar."""

    POLLINATOR = "pollinator", "Pollinator"


@dataclass(frozen=True)
class VolunteerTierInfo:
    tier: VolunteerTier
    # Earned by crawling on days_per_month days of a calendar month, where a day counts once
    # the user has submitted min_results_per_day results that day.
    days_per_month: int
    min_results_per_day: int
    perks: list[str]


# Lowest first: a user holding several has the last one listed.
VOLUNTEER_TIERS = [
    VolunteerTierInfo(
        tier=VolunteerTier.POLLINATOR,
        days_per_month=3,
        min_results_per_day=100,
        perks=[
            "Access to the members area in Matrix and Discord",
            "300 Seed Search queries a month to enhance our index",
        ],
    ),
]


def product_ids() -> dict[MembershipTier, str]:
    return {
        MembershipTier.SPROUT: settings.POLAR_PRODUCT_ID_SPROUT,
        MembershipTier.SAPLING: settings.POLAR_PRODUCT_ID_SAPLING,
        MembershipTier.CANOPY: settings.POLAR_PRODUCT_ID_CANOPY,
    }


def tier_for_product(product_id: str) -> MembershipTier | None:
    """The membership tier sold by a Polar product, or None if it isn't a membership product."""
    tiers_by_product = {product: tier for tier, product in product_ids().items() if product}
    return tiers_by_product.get(product_id)


def combined_search_monthly_limit(tier: str | None) -> int:
    """Combined Search queries a month for a web (JWT) caller with this membership or volunteer tier."""
    return settings.COMBINED_SEARCH_MONTHLY_LIMITS[tier or "free"]


AVERAGE_MONTH = timedelta(days=365.25 / 12)


def merge_support_spans(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping spans, so that time covered by two subscriptions at once, as when a member
    changes tier, counts once."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def supported_months(spans: list[tuple[datetime, datetime]]) -> int:
    """Months covered by the spans, ignoring gaps between them, to the nearest month so that a
    subscription from the 1st to the 1st counts as the months it paid for whatever their lengths.

    At least 1 if there are any spans, so that someone who has just subscribed isn't shown as never having been
    a supporter."""
    if not spans:
        return 0
    total = sum((end - start for start, end in merge_support_spans(spans)), timedelta())
    return max(1, round(total / AVERAGE_MONTH))
