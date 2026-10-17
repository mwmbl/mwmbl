"""Free Sprout memberships for volunteers who crawl, and the tier a user gets from either kind of membership."""

import calendar
from datetime import date

from django.conf import settings

from mwmbl.membership import MembershipTier
from mwmbl.models import CrawlerMembership, Membership, MwmblUser, UserStats
from mwmbl.utils import utc_today


def end_of_next_month(day: date) -> date:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, calendar.monthrange(year, month)[1])


def update_crawler_membership(user: MwmblUser, today: date) -> None:
    """Grant or extend the user's crawler membership if they have crawled on enough days this month.

    Called when the user's first results of the day arrive, so the membership starts the moment
    they reach the threshold.
    """
    days_crawled = UserStats.objects.filter(user=user, date__gte=today.replace(day=1), date__lte=today).count()
    if days_crawled < settings.CRAWLER_MEMBERSHIP_DAYS:
        return
    CrawlerMembership.objects.update_or_create(user=user, defaults={"valid_until": end_of_next_month(today)})


def has_crawler_membership(user_id: int) -> bool:
    return CrawlerMembership.objects.filter(user_id=user_id, valid_until__gte=utc_today()).exists()


def membership_tier(user_id: int) -> str | None:
    """The user's tier: their paid one if they have it, Sprout if they've earned it by crawling, else None.

    Every paid tier includes everything in Sprout, so a paid membership always wins.
    """
    paid_tier = Membership.objects.filter(user_id=user_id).values_list("tier", flat=True).first()
    if paid_tier is not None:
        return paid_tier
    return MembershipTier.SPROUT.value if has_crawler_membership(user_id) else None
