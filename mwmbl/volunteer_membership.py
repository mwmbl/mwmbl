"""Volunteer tiers earned by crawling, and the Combined Search limit a user gets from them and their paid membership."""

import calendar
from datetime import date

from mwmbl.membership import VOLUNTEER_TIERS, combined_search_monthly_limit
from mwmbl.models import Membership, MwmblUser, UserStats, VolunteerMembership
from mwmbl.utils import utc_today

TIER_RANK = {info.tier.value: rank for rank, info in enumerate(VOLUNTEER_TIERS)}


def end_of_next_month(day: date) -> date:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, calendar.monthrange(year, month)[1])


def update_volunteer_memberships(user: MwmblUser, today: date) -> None:
    """Grant or extend each volunteer tier the user has earned by crawling this month.

    Called after every batch of results, so a tier starts the moment the user reaches its
    threshold, and a grant that failed is retried on their next batch. Once this month has earned
    every tier, it costs one query. A grant never shortens a membership, so a longer one set by
    hand in the admin survives.
    """
    valid_until = end_of_next_month(today)
    extended = set(
        VolunteerMembership.objects.filter(user=user, valid_until__gte=valid_until).values_list("tier", flat=True)
    )
    pending = [info for info in VOLUNTEER_TIERS if info.tier not in extended]
    if not pending:
        return

    daily_results = list(
        UserStats.objects.filter(user=user, date__gte=today.replace(day=1), date__lte=today).values_list(
            "num_results", flat=True
        )
    )
    for info in pending:
        days_crawled = sum(num_results >= info.min_results_per_day for num_results in daily_results)
        if days_crawled < info.days_per_month:
            continue
        _, created = VolunteerMembership.objects.get_or_create(
            user=user, tier=info.tier, defaults={"valid_until": valid_until}
        )
        if not created:
            VolunteerMembership.objects.filter(user=user, tier=info.tier, valid_until__lt=valid_until).update(
                valid_until=valid_until
            )


def current_volunteer_memberships(user_ids) -> dict[int, VolunteerMembership]:
    """Each user's highest unexpired volunteer membership, for those who have one."""
    best = {}
    for volunteer_membership in VolunteerMembership.objects.filter(user_id__in=user_ids, valid_until__gte=utc_today()):
        held = best.get(volunteer_membership.user_id)
        if held is None or TIER_RANK[volunteer_membership.tier] > TIER_RANK[held.tier]:
            best[volunteer_membership.user_id] = volunteer_membership
    return best


def current_volunteer_membership(user_id: int) -> VolunteerMembership | None:
    return current_volunteer_memberships([user_id]).get(user_id)


def user_combined_search_monthly_limit(user_id: int) -> int:
    """The more generous of the limits from the user's paid membership and their volunteer tier.

    Volunteer allowances are set independently of the paid tiers, so neither always wins.
    """
    paid_tier = Membership.objects.filter(user_id=user_id).values_list("tier", flat=True).first()
    volunteer_membership = current_volunteer_membership(user_id)
    volunteer_tier = volunteer_membership.tier if volunteer_membership else None
    return max(combined_search_monthly_limit(paid_tier), combined_search_monthly_limit(volunteer_tier))
