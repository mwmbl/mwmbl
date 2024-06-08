import gzip
from datetime import datetime, timedelta
from glob import glob
from itertools import islice
from logging import getLogger
from pathlib import Path
from urllib.parse import urlparse

import django
from django.conf import settings
from pydantic import BaseModel
from redis import Redis

from mwmbl.count_urls import get_counts, get_domain_result_count
from mwmbl.crawler.batch import HashedBatch
from mwmbl.crawler.urls import URLDatabase
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.update_urls import get_datetime_from_timestamp

logger = getLogger(__name__)

URL_DATE_COUNT_KEY = "url-count-{date}"
URL_HOUR_COUNT_KEY = "url-count-hour-{hour}"
USERS_KEY = "users-{date}"
USER_COUNT_KEY = "user-count-{date}"
HOST_COUNT_KEY = "host-count-{date}"
HOST_COUNT_ALL_KEY = "host-count-all-{date}"
HOST_COUNT_LINK_KEY = "host-count-link-{date}"
HOST_COUNT_LINK_NEW_KEY = "host-count-link-new-{date}"
SHORT_EXPIRE_SECONDS = 60 * 60 * 24
LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30


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
    top_users: dict[str, int]
    top_domains: dict[str, int]
    urls_in_index_daily: dict[str, int]
    domains_in_index_daily: dict[str, int]
    results_in_index_daily: dict[str, int]


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

    def record_batch(self, hashed_batch: HashedBatch):
        date_time = get_datetime_from_timestamp(hashed_batch.timestamp)

        num_crawled_urls = sum(1 for item in hashed_batch.items if item.content is not None)

        date = date_time.date()
        url_count_key = URL_DATE_COUNT_KEY.format(date=date)
        self.redis.incrby(url_count_key, num_crawled_urls)
        self.redis.expire(url_count_key, LONG_EXPIRE_SECONDS)

        print("Date time", date_time)
        hour = datetime(date_time.year, date_time.month, date_time.day, date_time.hour)
        hour_key = URL_HOUR_COUNT_KEY.format(hour=hour)
        self.redis.incrby(hour_key, num_crawled_urls)
        self.redis.expire(hour_key, SHORT_EXPIRE_SECONDS)

        users_key = USERS_KEY.format(date=date)
        self.redis.sadd(users_key, hashed_batch.user_id_hash)
        self.redis.expire(users_key, LONG_EXPIRE_SECONDS)

        user_count_key = USER_COUNT_KEY.format(date=date)
        self.redis.zincrby(user_count_key, num_crawled_urls, hashed_batch.user_id_hash)
        self.redis.expire(user_count_key, SHORT_EXPIRE_SECONDS)

        host_key = HOST_COUNT_KEY.format(date=date)
        host_all_key = HOST_COUNT_ALL_KEY.format(date=date)
        with URLDatabase() as url_db:
            for item in hashed_batch.items:
                host = urlparse(item.url).netloc
                self.redis.zincrby(host_all_key, 1, host)

                if item.content is None:
                    continue

                self.redis.zincrby(host_key, 1, host)

                for link in item.content.links:
                    link_host = urlparse(link).netloc
                    self.redis.zincrby(HOST_COUNT_LINK_KEY.format(date=date), 1, link_host)

                    if link not in url_db:
                        self.redis.zincrby(HOST_COUNT_LINK_NEW_KEY.format(date=date), 1, link_host)

        self.redis.expire(host_key, SHORT_EXPIRE_SECONDS)
        self.redis.expire(host_all_key, SHORT_EXPIRE_SECONDS)

    def get_stats(self) -> MwmblStats:
        date_time = datetime.utcnow()
        date = date_time.date()

        urls_crawled_daily = {}
        users_crawled_daily = {}
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

        hour_counts = []
        for i in range(date_time.hour + 1):
            hour = datetime(date_time.year, date_time.month, date_time.day, i)
            hour_key = URL_HOUR_COUNT_KEY.format(hour=hour)
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
        return MwmblStats(
            urls_crawled_today=urls_crawled_today,
            urls_crawled_daily=urls_crawled_daily,
            urls_crawled_hourly=hour_counts,
            users_crawled_daily=users_crawled_daily,
            top_users=user_counts,
            top_domains=host_counts,
            **index_stats,
        )

    def get_domain_stats(self) -> list[DomainStats]:
        date_time = datetime.utcnow()
        host_all_key = HOST_COUNT_ALL_KEY.format(date=date_time.date())
        host_counts_all = self.redis.zrevrange(host_all_key, 0, 1000, withscores=True)
        all_domain_stats = []
        for host, count in host_counts_all:
            num_successful = self.redis.zscore(HOST_COUNT_KEY.format(date=date_time.date()), host)
            num_links = self.redis.zscore(HOST_COUNT_LINK_KEY.format(date=date_time.date()), host)
            num_links_new = self.redis.zscore(HOST_COUNT_LINK_NEW_KEY.format(date=date_time.date()), host)
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
        date_time = datetime.utcnow()
        num_crawled = self.redis.zscore(HOST_COUNT_ALL_KEY.format(date=date_time.date()), host)
        num_successful = self.redis.zscore(HOST_COUNT_KEY.format(date=date_time.date()), host)
        num_links = self.redis.zscore(HOST_COUNT_LINK_KEY.format(date=date_time.date()), host)
        num_links_new = self.redis.zscore(HOST_COUNT_LINK_NEW_KEY.format(date=date_time.date()), host)
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


def get_test_batches():
    for path in glob("./devdata/batches/**/*.json.gz", recursive=True):
        print("Processing path", path)
        with gzip.open(path) as gzip_file:
            yield HashedBatch.parse_raw(gzip_file.read())


if __name__ == '__main__':
    django.setup()
    redis = Redis(host='localhost', port=6379, decode_responses=True)
    stats = StatsManager(redis)
    batches = get_test_batches()
    start = datetime.now()
    processed = 0
    for batch in islice(batches, 10000):
        stats.record_batch(batch)
        processed += 1
    total_time = (datetime.now() - start).total_seconds()
    print("Processed", processed)
    print("Total time", total_time)
    print("Time per batch", total_time/processed)
