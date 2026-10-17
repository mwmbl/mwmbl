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

    Called after every batch of results, so the membership starts the moment the user reaches the
    threshold, and a grant that failed is retried on their next batch. Once this month has earned
    the membership, it costs one query. A grant never shortens a membership, so a longer one set by
    hand in the admin survives.
    """
    valid_until = end_of_next_month(today)
    if CrawlerMembership.objects.filter(user=user, valid_until__gte=valid_until).exists():
        return

    days_crawled = UserStats.objects.filter(
        user=user,
        date__gte=today.replace(day=1),
        date__lte=today,
        num_results__gte=settings.CRAWLER_MEMBERSHIP_MIN_RESULTS,
    ).count()
    if days_crawled < settings.CRAWLER_MEMBERSHIP_DAYS:
        return

    _, created = CrawlerMembership.objects.get_or_create(user=user, defaults={"valid_until": valid_until})
    if not created:
        CrawlerMembership.objects.filter(user=user, valid_until__lt=valid_until).update(valid_until=valid_until)


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
