from datetime import date, datetime, timedelta, timezone
from logging import getLogger

from django.conf import settings
from django.db import models
from pydantic import BaseModel
from redis import Redis

from mwmbl.count_urls import get_counts, get_domain_result_count
from mwmbl.crawler.batch import Results
from mwmbl.models import DailyCrawlerStats, MwmblUser, UserStats
from mwmbl.utils import utc_today

logger = getLogger(__name__)

# Redis key constants for daily crawler stats
USERS_KEY = "crawler:users:{date}"
RESULTS_COUNT_KEY = "crawler:results:{date}"
USER_RESULTS_COUNT_KEY = "crawler:user-results:{date}:{username}"
DATASET_QUERIES_COUNT_KEY = "crawler:dataset-queries:{date}"
DATASET_RESULTS_COUNT_KEY = "crawler:dataset-results:{date}"
BLACKLISTED_REMOVED_COUNT_KEY = "crawler:blacklisted-removed:{date}"
ALL_TIME_LEADERBOARD_KEY = "crawler:all-time-leaderboard"

LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30


def get_redis() -> Redis:
    """Get a Redis connection for stats."""
    return Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=5,
        socket_timeout=5,
        decode_responses=True,
    )


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
    def __init__(self, redis_client: Redis | None = None):
        self._redis = redis_client

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def get_stats(self) -> MwmblStats:
        date_time = datetime.now(timezone.utc)
        date = date_time.date()

        users_crawled_daily = {}
        results_indexed_daily = {}
        dataset_queries_daily = {}
        dataset_results_daily = {}
        blacklisted_results_removed_daily = {}

        # Try to read from Redis first (for today and recent days)
        # Fall back to Postgres for historical data
        thirty_days_ago = date - timedelta(days=29)
        daily_stats = DailyCrawlerStats.objects.filter(date__gte=thirty_days_ago).order_by("date")
        pg_stats_by_date = {str(stat.date): stat for stat in daily_stats}

        for i in range(29, -1, -1):
            date_i = date - timedelta(days=i)
            date_str = str(date_i)

            # Try Redis first for today and yesterday (most likely to have fresh data)
            if i <= 1:
                redis_users = self.redis.get(USERS_KEY.format(date=date_str))
                redis_results = self.redis.get(RESULTS_COUNT_KEY.format(date=date_str))
                redis_dataset_queries = self.redis.get(DATASET_QUERIES_COUNT_KEY.format(date=date_str))
                redis_dataset_results = self.redis.get(DATASET_RESULTS_COUNT_KEY.format(date=date_str))
                redis_blacklisted = self.redis.get(BLACKLISTED_REMOVED_COUNT_KEY.format(date=date_str))

                if redis_users is not None:
                    users_crawled_daily[date_str] = int(redis_users)
                elif pg_stats_by_date.get(date_str):
                    users_crawled_daily[date_str] = pg_stats_by_date[date_str].users_crawled
                else:
                    users_crawled_daily[date_str] = 0

                if redis_results is not None:
                    results_indexed_daily[date_str] = int(redis_results)
                elif pg_stats_by_date.get(date_str):
                    results_indexed_daily[date_str] = pg_stats_by_date[date_str].results_indexed
                else:
                    results_indexed_daily[date_str] = 0

                if redis_dataset_queries is not None:
                    dataset_queries_daily[date_str] = int(redis_dataset_queries)
                elif pg_stats_by_date.get(date_str):
                    dataset_queries_daily[date_str] = pg_stats_by_date[date_str].dataset_queries
                else:
                    dataset_queries_daily[date_str] = 0

                if redis_dataset_results is not None:
                    dataset_results_daily[date_str] = int(redis_dataset_results)
                elif pg_stats_by_date.get(date_str):
                    dataset_results_daily[date_str] = pg_stats_by_date[date_str].dataset_results
                else:
                    dataset_results_daily[date_str] = 0

                if redis_blacklisted is not None:
                    blacklisted_results_removed_daily[date_str] = int(redis_blacklisted)
                elif pg_stats_by_date.get(date_str):
                    blacklisted_results_removed_daily[date_str] = pg_stats_by_date[date_str].blacklisted_results_removed
                else:
                    blacklisted_results_removed_daily[date_str] = 0
            else:
                # For older days, use Postgres
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

        # Get today's leaderboard from Redis cache, fall back to Postgres
        today_leaderboard = self._get_today_leaderboard(date)
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

    def _get_today_leaderboard(self, target_date: date) -> list[tuple[str, int]]:
        """Get today's leaderboard from Redis cache, fall back to Postgres."""
        # Try to get from Redis cache first
        cached = self.redis.get(ALL_TIME_LEADERBOARD_KEY)
        if cached:
            try:
                import json

                json.loads(cached)
                # Filter to today's top users (the cache stores all-time, but we want today's)
                # For now, fall back to Postgres for today's leaderboard
                pass
            except Exception:
                pass

        # Fall back to Postgres for today's leaderboard
        return self.get_leaderboard_for_date(target_date)

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
        today_str = str(today)

        # Use Redis for atomic increments (avoids race conditions)
        pipe = self.redis.pipeline()

        # Increment user's results for today
        user_key = USER_RESULTS_COUNT_KEY.format(date=today_str, username=user.username)
        pipe.incrby(user_key, num_results)
        pipe.expire(user_key, LONG_EXPIRE_SECONDS)

        # Increment total results indexed today
        pipe.incrby(RESULTS_COUNT_KEY.format(date=today_str), num_results)
        pipe.expire(RESULTS_COUNT_KEY.format(date=today_str), LONG_EXPIRE_SECONDS)

        # Check if this is a new user for today (using Redis set)
        user_set_key = f"crawler:users:set:{today_str}"
        pipe.sadd(user_set_key, user.username)
        pipe.expire(user_set_key, LONG_EXPIRE_SECONDS)

        # Execute pipeline and get results
        pipe_results = pipe.execute()
        # sadd result is at index 4 (0: incrby, 1: expire, 2: incrby, 3: expire, 4: sadd, 5: expire)
        is_new_user = pipe_results[4]

        # If new user for today, increment users_crawled
        if is_new_user:
            self.redis.incr(USERS_KEY.format(date=today_str))
            self.redis.expire(USERS_KEY.format(date=today_str), LONG_EXPIRE_SECONDS)

        # Also persist to Postgres for all-time leaderboard (UserStats)
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

        # Invalidate all-time leaderboard cache
        self.redis.delete(ALL_TIME_LEADERBOARD_KEY)

    def record_blacklisted_removed(self, num_results: int) -> None:
        """Record documents removed from the index by the background blacklist purge.

        This is the only visibility we have on the purge loop: retrieval filters
        blacklisted domains out of results whether or not the removal ever happens, so
        a count that stays at zero while queries are being filtered means the loop is
        broken.
        """
        today = utc_today()
        today_str = str(today)

        # Use Redis for atomic increment
        self.redis.incrby(BLACKLISTED_REMOVED_COUNT_KEY.format(date=today_str), num_results)
        self.redis.expire(BLACKLISTED_REMOVED_COUNT_KEY.format(date=today_str), LONG_EXPIRE_SECONDS)

        # Also persist to Postgres
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

        dataset_date_str = str(dataset_date)

        # Count queries
        num_queries = len(hashed_dataset.queryDataset)

        # Count successful search results (exclude unsuccessful attempts)
        num_successful_results = 0
        for search_result_set in hashed_dataset.searchResults:
            if search_result_set.success:
                num_successful_results += len(search_result_set.results)

        # Use Redis for atomic increments
        pipe = self.redis.pipeline()
        pipe.incrby(DATASET_QUERIES_COUNT_KEY.format(date=dataset_date_str), num_queries)
        pipe.expire(DATASET_QUERIES_COUNT_KEY.format(date=dataset_date_str), LONG_EXPIRE_SECONDS)
        pipe.incrby(DATASET_RESULTS_COUNT_KEY.format(date=dataset_date_str), num_successful_results)
        pipe.expire(DATASET_RESULTS_COUNT_KEY.format(date=dataset_date_str), LONG_EXPIRE_SECONDS)
        pipe.execute()

        # Also persist to Postgres
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

    def sync_to_postgres(self, target_date: date | None = None) -> None:
        """
        Sync Redis crawler stats to Postgres (DailyCrawlerStats) for historical persistence.

        Reads the daily counters from Redis and persists them to the DailyCrawlerStats table.
        If target_date is None, syncs yesterday's data (today's data is still being written).
        """
        from mwmbl.models import DailyCrawlerStats
        from mwmbl.utils import utc_today

        if target_date is None:
            target_date = utc_today() - timedelta(days=1)

        date_str = str(target_date)

        # Read all counters from Redis
        users_crawled = self.redis.get(USERS_KEY.format(date=date_str))
        results_indexed = self.redis.get(RESULTS_COUNT_KEY.format(date=date_str))
        dataset_queries = self.redis.get(DATASET_QUERIES_COUNT_KEY.format(date=date_str))
        dataset_results = self.redis.get(DATASET_RESULTS_COUNT_KEY.format(date=date_str))
        blacklisted_removed = self.redis.get(BLACKLISTED_REMOVED_COUNT_KEY.format(date=date_str))

        # Convert to int, defaulting to 0
        users_crawled = int(users_crawled) if users_crawled is not None else 0
        results_indexed = int(results_indexed) if results_indexed is not None else 0
        dataset_queries = int(dataset_queries) if dataset_queries is not None else 0
        dataset_results = int(dataset_results) if dataset_results is not None else 0
        blacklisted_removed = int(blacklisted_removed) if blacklisted_removed is not None else 0

        # Persist to Postgres
        DailyCrawlerStats.objects.update_or_create(
            date=target_date,
            defaults={
                "users_crawled": users_crawled,
                "results_indexed": results_indexed,
                "dataset_queries": dataset_queries,
                "dataset_results": dataset_results,
                "blacklisted_results_removed": blacklisted_removed,
            },
        )

        logger.info(f"Crawler stats synced to Postgres for {date_str}")

    def cache_all_time_leaderboard(self) -> None:
        """Compute all-time leaderboard from Postgres and cache in Redis."""
        leaderboard = self.get_all_time_leaderboard()
        import json

        self.redis.setex(ALL_TIME_LEADERBOARD_KEY, LONG_EXPIRE_SECONDS, json.dumps(leaderboard))
        logger.info("All-time leaderboard cached in Redis")
