from datetime import date, datetime, timedelta, timezone
from logging import getLogger

from pydantic import BaseModel
from redis import Redis

from mwmbl.count_urls import get_counts, get_domain_result_count
from mwmbl.crawler.batch import Results
from mwmbl.utils import utc_today

logger = getLogger(__name__)

URL_DATE_COUNT_KEY = "url-count-{date}"
URL_HOUR_COUNT_KEY = "url-count-hour-{hour}"
USERS_KEY = "users-{date}"
USER_COUNT_KEY = "user-count-{date}"
HOST_COUNT_KEY = "host-count-{date}"
HOST_COUNT_ALL_KEY = "host-count-all-{date}"
HOST_COUNT_LINK_KEY = "host-count-link-{date}"
HOST_COUNT_LINK_NEW_KEY = "host-count-link-new-{date}"
RESULTS_COUNT_KEY = "results-count-{date}"
USER_RESULTS_COUNT_KEY = "user-results-count-{date}"
DATASET_QUERIES_COUNT_KEY = "dataset-queries-count-{date}"
DATASET_RESULTS_COUNT_KEY = "dataset-results-count-{date}"
BLACKLISTED_REMOVED_COUNT_KEY = "blacklisted-removed-count-{date}"

SHORT_EXPIRE_SECONDS = 60 * 60 * 24
LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30


def hour_count_key(date_time: datetime) -> str:
    """Key naming the hour a crawl happened in, e.g. ``url-count-hour-2026-09-08 13:00:00``.

    Formatted rather than built from a truncated datetime so that the key does not depend
    on whether ``date_time`` is aware: ``str()`` of an aware datetime carries a ``+00:00``
    suffix, which would split the reader in :meth:`StatsManager.get_stats` and zero the hourly chart.
    """
    return URL_HOUR_COUNT_KEY.format(hour=date_time.strftime("%Y-%m-%d %H:00:00"))


class DomainStats(BaseModel):
    domain_name: str
    num_crawled: int
    num_successful: int
    num_links: int
    num_links_new: int
    num_index_results: int


class MwmblStats(BaseModel):
    urls_crawled_today: int
    urls_crawled_daily: dict[str, int]
    urls_crawled_hourly: list[int]
    users_crawled_daily: dict[str, int]
    top_users: list[tuple[str, int]]
    top_domains: list[tuple[str, int]]
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

        urls_crawled_daily = {}
        users_crawled_daily = {}
        results_indexed_daily = {}
        dataset_queries_daily = {}
        dataset_results_daily = {}
        blacklisted_results_removed_daily = {}
        for i in range(29, -1, -1):
            date_i = date - timedelta(days=i)
            url_count_key = URL_DATE_COUNT_KEY.format(date=date_i)
            url_count = self.redis.get(url_count_key)
            if url_count is None:
                url_count = 0
            urls_crawled_daily[str(date_i)] = url_count

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

        hour_counts = []
        for i in range(date_time.hour + 1):
            hour_key = hour_count_key(date_time.replace(hour=i))
            hour_count = self.redis.get(hour_key)
            if hour_count is None:
                hour_count = 0
            hour_counts.append(hour_count)

        user_count_key = USER_COUNT_KEY.format(date=date_time.date())
        user_counts = self.redis.zrevrange(user_count_key, 0, 100, withscores=True)

        host_key = HOST_COUNT_KEY.format(date=date_time.date())
        host_counts = self.redis.zrevrange(host_key, 0, 100, withscores=True)

        urls_crawled_today = list(urls_crawled_daily.values())[-1]
        index_stats = get_counts()

        user_results_count_key = USER_RESULTS_COUNT_KEY.format(date=date_time.date())
        user_results_counts = self.redis.zrevrange(user_results_count_key, 0, 100, withscores=True)

        return MwmblStats(
            urls_crawled_today=urls_crawled_today,
            urls_crawled_daily=urls_crawled_daily,
            urls_crawled_hourly=hour_counts,
            users_crawled_daily=users_crawled_daily,
            top_users=user_counts,
            top_domains=host_counts,
            results_indexed_daily=results_indexed_daily,
            top_user_results=user_results_counts,
            dataset_queries_daily=dataset_queries_daily,
            dataset_results_daily=dataset_results_daily,
            blacklisted_results_removed_daily=blacklisted_results_removed_daily,
            **index_stats,
        )

    def get_user_stats(self, username: str) -> dict:
        """Per-user stats for the last 30 days.

        Reads the per-user results-indexed sorted set for each day. Crawled and
        blacklisted counts are only tracked per-user on the legacy hash path, not
        per-username, so this exposes the user's indexed results  only.
        """
        date = utc_today()
        results_indexed_daily = {}
        for i in range(29, -1, -1):
            date_i = date - timedelta(days=i)
            user_result_count_key = USER_RESULTS_COUNT_KEY.format(date=date_i)
            count = self.redis.zscore(user_result_count_key, username)
            results_indexed_daily[str(date_i)] = int(count) if count else 0

        return {
            "username": username,
            "results_indexed_daily": results_indexed_daily,
            "results_indexed_today": results_indexed_daily[str(date)],
        }

    def get_domain_stats(self) -> list[DomainStats]:
        today = utc_today()
        host_all_key = HOST_COUNT_ALL_KEY.format(date=today)
        host_counts_all = self.redis.zrevrange(host_all_key, 0, 1000, withscores=True)
        all_domain_stats = []
        for host, count in host_counts_all:
            num_successful = self.redis.zscore(HOST_COUNT_KEY.format(date=today), host)
            num_links = self.redis.zscore(HOST_COUNT_LINK_KEY.format(date=today), host)
            num_links_new = self.redis.zscore(HOST_COUNT_LINK_NEW_KEY.format(date=today), host)
            num_index_results = get_domain_result_count(host)
            domain_stats = DomainStats(
                domain_name=host,
                num_crawled=count,
                num_successful=num_successful or 0,
                num_links=num_links or 0,
                num_links_new=num_links_new or 0,
                num_index_results=num_index_results,
            )
            all_domain_stats.append(domain_stats)
        return all_domain_stats

    def get_stats_for_domain(self, host: str) -> DomainStats:
        today = utc_today()
        num_crawled = self.redis.zscore(HOST_COUNT_ALL_KEY.format(date=today), host)
        num_successful = self.redis.zscore(HOST_COUNT_KEY.format(date=today), host)
        num_links = self.redis.zscore(HOST_COUNT_LINK_KEY.format(date=today), host)
        num_links_new = self.redis.zscore(HOST_COUNT_LINK_NEW_KEY.format(date=today), host)
        num_index_results = get_domain_result_count(host)
        domain_stats = DomainStats(
            domain_name=host,
            num_crawled=num_crawled or 0,
            num_successful=num_successful or 0,
            num_links=num_links or 0,
            num_links_new=num_links_new or 0,
            num_index_results=num_index_results,
        )
        return domain_stats

    def record_results(self, results: Results, username: str) -> None:
        result_count_key = RESULTS_COUNT_KEY.format(date=utc_today())
        num_results = len(results.results)
        self.redis.incrby(result_count_key, num_results)
        self.redis.expire(result_count_key, LONG_EXPIRE_SECONDS)

        user_result_count_key = USER_RESULTS_COUNT_KEY.format(date=utc_today())
        self.redis.zincrby(user_result_count_key, num_results, username)
        self.redis.expire(user_result_count_key, SHORT_EXPIRE_SECONDS)

        users_key = USERS_KEY.format(date=utc_today())
        self.redis.sadd(users_key, username)
        self.redis.expire(users_key, LONG_EXPIRE_SECONDS)

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
