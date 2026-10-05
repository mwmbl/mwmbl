from datetime import date, datetime, timedelta, timezone
from logging import getLogger

from django.db import models
from pydantic import BaseModel
from redis import Redis

from mwmbl.count_urls import get_counts, get_domain_result_count
from mwmbl.crawler.batch import Results
from mwmbl.models import MwmblUser, UserStats
from mwmbl.utils import utc_today

logger = getLogger(__name__)

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
    def __init__(self, redis: Redis):
        self.redis = redis

    def get_stats(self) -> MwmblStats:
        date_time = datetime.now(timezone.utc)
        date = date_time.date()

        users_crawled_daily = {}
        results_indexed_daily = {}
        dataset_queries_daily = {}
        dataset_results_daily = {}
        blacklisted_results_removed_daily = {}
        for i in range(29, -1, -1):
            date_i = date - timedelta(days=i)

            user_day_count_key = USERS_KEY.format(date=date_i)
            user_day_count = self.redis.scard(user_day_count_key)
            users_crawled_daily[str(date_i)] = user_day_count

            result_count_key = RESULTS_COUNT_KEY.format(date=date_i)
            result_count = self.redis.get(result_count_key)
            if result_count is None:
                result_count = 0
            results_indexed_daily[str(date_i)] = result_count

            dataset_queries_count_key = DATASET_QUERIES_COUNT_KEY.format(date=date_i)
            dataset_queries_count = self.redis.get(dataset_queries_count_key)
            if dataset_queries_count is None:
                dataset_queries_count = 0
            dataset_queries_daily[str(date_i)] = dataset_queries_count

            dataset_results_count_key = DATASET_RESULTS_COUNT_KEY.format(date=date_i)
            dataset_results_count = self.redis.get(dataset_results_count_key)
            if dataset_results_count is None:
                dataset_results_count = 0
            dataset_results_daily[str(date_i)] = dataset_results_count

            blacklisted_removed_count_key = BLACKLISTED_REMOVED_COUNT_KEY.format(date=date_i)
            blacklisted_removed_count = self.redis.get(blacklisted_removed_count_key)
            if blacklisted_removed_count is None:
                blacklisted_removed_count = 0
            blacklisted_results_removed_daily[str(date_i)] = blacklisted_removed_count

        index_stats = get_counts()

        user_results_count_key = USER_RESULTS_COUNT_KEY.format(date=date_time.date())
        user_results_counts = self.redis.zrevrange(user_results_count_key, 0, 100, withscores=True)

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

        result_count_key = RESULTS_COUNT_KEY.format(date=today)
        self.redis.incrby(result_count_key, num_results)
        self.redis.expire(result_count_key, LONG_EXPIRE_SECONDS)

        user_result_count_key = USER_RESULTS_COUNT_KEY.format(date=today)
        self.redis.zincrby(user_result_count_key, num_results, user.username)
        self.redis.expire(user_result_count_key, LONG_EXPIRE_SECONDS)

        users_key = USERS_KEY.format(date=today)
        self.redis.sadd(users_key, user.username)
        self.redis.expire(users_key, LONG_EXPIRE_SECONDS)

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
        dataset_queries_count_key = DATASET_QUERIES_COUNT_KEY.format(date=dataset_date)
        self.redis.incrby(dataset_queries_count_key, num_queries)
        self.redis.expire(dataset_queries_count_key, LONG_EXPIRE_SECONDS)

        # Count successful search results (exclude unsuccessful attempts)
        num_successful_results = 0
        for search_result_set in hashed_dataset.searchResults:
            if search_result_set.success:
                num_successful_results += len(search_result_set.results)

        dataset_results_count_key = DATASET_RESULTS_COUNT_KEY.format(date=dataset_date)
        self.redis.incrby(dataset_results_count_key, num_successful_results)
        self.redis.expire(dataset_results_count_key, LONG_EXPIRE_SECONDS)
