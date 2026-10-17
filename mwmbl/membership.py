"""Membership tiers: supporter subscriptions sold through Polar, separate from metered API billing."""

from dataclasses import dataclass

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
