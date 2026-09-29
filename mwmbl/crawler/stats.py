from datetime import date, datetime, timedelta, timezone
from logging import getLogger

from django.db import models
from pydantic import BaseModel

from mwmbl.count_urls import get_counts, get_domain_result_count
from mwmbl.crawler.batch import Results
from mwmbl.models import DailyCrawlerStats, MwmblUser, UserStats
from mwmbl.utils import utc_today

logger = getLogger(__name__)

# Redis key constants (used by admin_views.py for reading blacklist status)
USERS_KEY = "users-{date}"
RESULTS_COUNT_KEY = "results-count-{date}"
USER_RESULTS_COUNT_KEY = "user-results-count-{date}"
DATASET_QUERIES_COUNT_KEY = "dataset-queries-count-{date}"
DATASET_RESULTS_COUNT_KEY = "dataset-results-count-{date}"
BLACKLISTED_REMOVED_COUNT_KEY = "blacklisted-removed-count-{date}"

LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30


class DomainStats(BaseModel):
    domain_name: str
    num_index_results: int


class MwmblStats(BaseModel):
    users_crawled_daily: dict[str, int]
    results_indexed_daily: dict[str, int]
    top_user_results: list[tuple[str, int]]
    urls_in_index_daily: dict[str, int]
    domains_in_index_daily: dict[str, int]
    results_in_index_daily: dict[str, int]
    dataset_queries_daily: dict[str, int]
    dataset_results_daily: dict[str, int]
    blacklisted_results_removed_daily: dict[str, int]


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
    def __init__(self):
        pass

    def get_stats(self) -> MwmblStats:
        date_time = datetime.now(timezone.utc)
        date = date_time.date()

        users_crawled_daily = {}
        results_indexed_daily = {}
        dataset_queries_daily = {}
        dataset_results_daily = {}
        blacklisted_results_removed_daily = {}

        # Get the last 30 days from Postgres
        thirty_days_ago = date - timedelta(days=29)
        daily_stats = DailyCrawlerStats.objects.filter(date__gte=thirty_days_ago).order_by("date")

        # Build a lookup dict from Postgres data
        pg_stats_by_date = {str(stat.date): stat for stat in daily_stats}

        for i in range(29, -1, -1):
            date_i = date - timedelta(days=i)
            date_str = str(date_i)

            pg_stat = pg_stats_by_date.get(date_str)
            if pg_stat:
                users_crawled_daily[date_str] = pg_stat.users_crawled
                results_indexed_daily[date_str] = pg_stat.results_indexed
                dataset_queries_daily[date_str] = pg_stat.dataset_queries
                dataset_results_daily[date_str] = pg_stat.dataset_results
                blacklisted_results_removed_daily[date_str] = pg_stat.blacklisted_results_removed
            else:
                users_crawled_daily[date_str] = 0
                results_indexed_daily[date_str] = 0
                dataset_queries_daily[date_str] = 0
                dataset_results_daily[date_str] = 0
                blacklisted_results_removed_daily[date_str] = 0

        index_stats = get_counts()

        # Get today's leaderboard from the UserStats table (Postgres)
        today_leaderboard = self.get_leaderboard_for_date(date)
        user_results_counts = [(username, float(score)) for username, score in today_leaderboard]

        return MwmblStats(
            users_crawled_daily=users_crawled_daily,
            results_indexed_daily=results_indexed_daily,
            top_user_results=user_results_counts,
            dataset_queries_daily=dataset_queries_daily,
            dataset_results_daily=dataset_results_daily,
            blacklisted_results_removed_daily=blacklisted_results_removed_daily,
            **index_stats,
        )

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
        from django.db.models import Sum

        results = (
            UserStats.objects.values("user__username")
            .annotate(total_results=Sum("num_results"))
            .order_by("-total_results")[:100]
        )
        return [(row["user__username"], row["total_results"]) for row in results]

    def record_results(self, results: Results, user: MwmblUser) -> None:
        today = utc_today()
        num_results = len(results.results)

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
            # This is a new user for today, increment users_crawled
            DailyCrawlerStats.objects.filter(date=today).update(
                users_crawled=models.F("users_crawled") + 1,
            )

        # Also persist aggregate daily stats to Postgres (DailyCrawlerStats)
        DailyCrawlerStats.objects.filter(date=today).update(
            results_indexed=models.F("results_indexed") + num_results,
        )
        # If the row didn't exist, create it
        if not DailyCrawlerStats.objects.filter(date=today).exists():
            DailyCrawlerStats.objects.create(
                date=today,
                users_crawled=1,
                results_indexed=num_results,
            )

    def record_blacklisted_removed(self, num_results: int) -> None:
        """Record documents removed from the index by the background blacklist purge.

        This is the only visibility we have on the purge loop: retrieval filters
        blacklisted domains out of results whether or not the removal ever happens, so
        a count that stays at zero while queries are being filtered means the loop is
        broken.
        """
        today = utc_today()

        # Persist to Postgres
        DailyCrawlerStats.objects.filter(date=today).update(
            blacklisted_results_removed=models.F("blacklisted_results_removed") + num_results,
        )
        if not DailyCrawlerStats.objects.filter(date=today).exists():
            DailyCrawlerStats.objects.create(
                date=today,
                blacklisted_results_removed=num_results,
            )

    def record_dataset(self, hashed_dataset) -> None:
        """Record dataset statistics from a dataset submission."""
        # Parse the date from the dataset
        try:
            dataset_date = date.fromisoformat(hashed_dataset.date)
        except ValueError:
            # If date parsing fails, use current date
            dataset_date = utc_today()

        # Count queries
        num_queries = len(hashed_dataset.queryDataset)

        # Count successful search results (exclude unsuccessful attempts)
        num_successful_results = 0
        for search_result_set in hashed_dataset.searchResults:
            if search_result_set.success:
                num_successful_results += len(search_result_set.results)

        # Persist to Postgres
        DailyCrawlerStats.objects.filter(date=dataset_date).update(
            dataset_queries=models.F("dataset_queries") + num_queries,
            dataset_results=models.F("dataset_results") + num_successful_results,
        )
        if not DailyCrawlerStats.objects.filter(date=dataset_date).exists():
            DailyCrawlerStats.objects.create(
                date=dataset_date,
                dataset_queries=num_queries,
                dataset_results=num_successful_results,
            )
