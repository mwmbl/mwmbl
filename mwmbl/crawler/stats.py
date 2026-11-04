import json
import re
from datetime import date, timedelta
from logging import getLogger

from django.db import models
from django.db.models import Sum
from pydantic import BaseModel
from redis import Redis

from mwmbl.count_urls import get_domain_result_count
from mwmbl.crawler.batch import MAX_ERROR_TAG_LENGTH, Results
from mwmbl.models import MwmblUser, UserStats
from mwmbl.utils import utc_today
from mwmbl.volunteer_membership import update_volunteer_memberships

logger = getLogger(__name__)

BLACKLISTED_REMOVED_COUNT_KEY = "blacklisted-removed-count-{date}"
CRAWLER_VERSION_RESULTS_KEY = "crawler-version-results-{date}"
CRAWLER_VERSION_DEVICES_KEY = "crawler-version-devices-{date}:{version}"
UNKNOWN_CRAWLER_VERSION = "unknown"
# ASCII digits only, and few of them: \d matches digits that int() rejects, and int() rejects
# very long digit strings.
RELEASED_CRAWLER_VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")

LEADERBOARD_CACHE_KEY = "leaderboard-{period}-{date}"

LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30
# The all-time board sums every user's daily rows - the heaviest query on the page - and a day's
# movement barely changes the top 100, so it is rebuilt once a day. Yesterday's board cannot change
# once the day is over, so it is cached for as long.
ALL_TIME_LEADERBOARD_CACHE_SECONDS = 60 * 60 * 24


class DomainStats(BaseModel):
    domain_name: str
    num_index_results: int


class CrawlerVersionDay(BaseModel):
    crawlers: int
    results: int


class CrawlerVersionRow(BaseModel):
    version: str
    days: list[CrawlerVersionDay]


class CrawlerVersionSpread(BaseModel):
    dates: list[date]
    rows: list[CrawlerVersionRow]
    total_crawlers: list[int]


def _version_sort_key(version: str) -> tuple[int, ...]:
    """Orders numerically, so 0.10.0 sorts above 0.9.0."""
    return tuple(int(part) for part in version.split("."))


# New stats we want per domain:
# - Number of results in index
# - Number of links to this domain in URL queue
# - Best score of links for this domain in URL queue
# - Number of URLs crawled for this domain today:
#   - Total - done
#   - Number of successes - done
#   - Number of timeouts
#   - Number of 404s
#   - Number excluded by robots.txt
#   - Number of other errors
# - Number of internal and external links to this domain crawled today
#   - Number of links excluded because they've already been crawled
# - Number of external links extracted from this domain today
# -


class StatsManager:
    def __init__(self, redis: Redis):
        self.redis = redis

    def get_cached_leaderboard(self, period: str) -> list[tuple[str, int]]:
        """The "yesterday" or "all" leaderboard, rebuilt at most once per UTC day."""
        cache_key = LEADERBOARD_CACHE_KEY.format(period=period, date=utc_today())
        cached = self.redis.get(cache_key)
        if cached is not None:
            return [(username, score) for username, score in json.loads(cached)]

        if period == "yesterday":
            leaderboard = self.get_leaderboard_for_date(utc_today() - timedelta(days=1))
        elif period == "all":
            leaderboard = self.get_all_time_leaderboard()
        else:
            raise ValueError(f"Unknown leaderboard period: {period}")
        self.redis.set(cache_key, json.dumps(leaderboard), ex=ALL_TIME_LEADERBOARD_CACHE_SECONDS)
        return leaderboard

    def get_user_stats(self, username: str) -> dict:
        """Per-user stats for the last 30 days from the UserStats table."""
        date = utc_today()
        results_indexed_daily = {}
        for i in range(29, -1, -1):
            date_i = date - timedelta(days=i)
            stat = UserStats.objects.filter(user__username=username, date=date_i).first()
            results_indexed_daily[str(date_i)] = stat.num_results if stat else 0

        return {
            "username": username,
            "results_indexed_daily": results_indexed_daily,
            "results_indexed_today": results_indexed_daily[str(date)],
        }

    def get_stats_for_domain(self, host: str) -> DomainStats:
        return DomainStats(domain_name=host, num_index_results=get_domain_result_count(host))

    def get_leaderboard_for_date(self, target_date: date) -> list[tuple[str, int]]:
        """Get leaderboard for a specific date from the UserStats table."""
        results = UserStats.objects.filter(date=target_date).select_related("user").order_by("-num_results")[:100]
        return [(stat.user.username, stat.num_results) for stat in results]

    def get_all_time_leaderboard(self) -> list[tuple[str, int]]:
        """Get all-time leaderboard by querying the UserStats table."""
        results = (
            UserStats.objects.values("user__username")
            .annotate(total_results=Sum("num_results"))
            .order_by("-total_results")[:100]
        )
        return [(row["user__username"], row["total_results"]) for row in results]

    def record_results(self, results: Results, user: MwmblUser) -> None:
        today = utc_today()
        num_results = len(results.results)

        self._record_crawler_version(results, user, today)

        # Persist to Postgres for all-time leaderboard.
        updated = UserStats.objects.filter(
            user=user,
            date=today,
        ).update(num_results=models.F("num_results") + num_results)

        if updated == 0:
            UserStats.objects.create(
                user=user,
                date=today,
                num_results=num_results,
            )

        update_volunteer_memberships(user, today)

    def _record_crawler_version(self, results: Results, user: MwmblUser, today: date) -> None:
        # The version is client-supplied, so only release-shaped ones get a label of their own:
        # anything else would let a client mint a new row, and new Redis keys, per request.
        claimed_version = results.crawler_version or ""
        version = claimed_version if RELEASED_CRAWLER_VERSION.fullmatch(claimed_version) else UNKNOWN_CRAWLER_VERSION

        version_results_key = CRAWLER_VERSION_RESULTS_KEY.format(date=today)
        self.redis.hincrby(version_results_key, version, len(results.results))
        self.redis.expire(version_results_key, LONG_EXPIRE_SECONDS)

        # A crawler is its user and device name, not its API key: one key can serve several
        # crawlers and one user can hold several keys. Crawlers sending no device name count
        # as one per user.
        device_name = (results.device_name or "")[:MAX_ERROR_TAG_LENGTH]
        crawler_id = f"{user.id}:{device_name}"
        version_devices_key = CRAWLER_VERSION_DEVICES_KEY.format(date=today, version=version)
        self.redis.sadd(version_devices_key, crawler_id)
        self.redis.expire(version_devices_key, LONG_EXPIRE_SECONDS)

    def get_crawler_version_spread(self, days: int) -> CrawlerVersionSpread:
        """Crawlers and results per crawler version for each of the last `days` days, oldest first.

        A crawler that changed version during a day is counted under both, so the per-day
        total is the distinct crawlers across all versions rather than the column sum.
        """
        today = utc_today()
        dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        results_by_date = {day: self.redis.hgetall(CRAWLER_VERSION_RESULTS_KEY.format(date=day)) for day in dates}

        all_versions = {version for counts in results_by_date.values() for version in counts}
        known_versions = sorted(all_versions - {UNKNOWN_CRAWLER_VERSION}, key=_version_sort_key, reverse=True)
        unknown_versions = [UNKNOWN_CRAWLER_VERSION] if UNKNOWN_CRAWLER_VERSION in all_versions else []

        ordered_versions = known_versions + unknown_versions
        pipeline = self.redis.pipeline(transaction=False)
        for version in ordered_versions:
            for day in dates:
                pipeline.scard(CRAWLER_VERSION_DEVICES_KEY.format(date=day, version=version))
        crawler_counts = iter(pipeline.execute())

        rows = []
        for version in ordered_versions:
            version_days = [
                CrawlerVersionDay(crawlers=next(crawler_counts), results=int(results_by_date[day].get(version, 0)))
                for day in dates
            ]
            rows.append(CrawlerVersionRow(version=version, days=version_days))

        total_crawlers = []
        for day in dates:
            day_keys = [
                CRAWLER_VERSION_DEVICES_KEY.format(date=day, version=version) for version in results_by_date[day]
            ]
            total_crawlers.append(len(self.redis.sunion(day_keys)) if day_keys else 0)

        return CrawlerVersionSpread(dates=dates, rows=rows, total_crawlers=total_crawlers)

    def record_blacklisted_removed(self, num_results: int) -> None:
        """Record documents removed from the index by the background blacklist purge.

        This is the only visibility we have on the purge loop: retrieval filters
        blacklisted domains out of results whether or not the removal ever happens, so
        a count that stays at zero while queries are being filtered means the loop is
        broken.
        """
        blacklisted_removed_count_key = BLACKLISTED_REMOVED_COUNT_KEY.format(date=utc_today())
        self.redis.incrby(blacklisted_removed_count_key, num_results)
        self.redis.expire(blacklisted_removed_count_key, LONG_EXPIRE_SECONDS)
