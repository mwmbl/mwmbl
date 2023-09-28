import gzip
import json
from datetime import datetime
from glob import glob
from itertools import islice
from logging import getLogger
from urllib.parse import urlparse

from redis import Redis

from mwmbl.crawler.batch import HashedBatch
from mwmbl.indexer.update_urls import get_datetime_from_timestamp

logger = getLogger(__name__)

URL_DATE_COUNT_KEY = "url-count-{date}"
URL_HOUR_COUNT_KEY = "url-count-hour-{hour}"
USER_COUNT_KEY = "user-count-{date}"
HOST_COUNT_KEY = "host-count-{date}"
EXPIRE_SECONDS = 60*60*24


class StatsManager:
    def __init__(self, redis: Redis):
        self.redis = redis

    def record_batch(self, hashed_batch: HashedBatch):
        date_time = get_datetime_from_timestamp(hashed_batch.timestamp)

        num_crawled_urls = sum(1 for item in hashed_batch.items if item.content is not None)

        url_count_key = URL_DATE_COUNT_KEY.format(date=date_time.date)
        self.redis.incrby(url_count_key, num_crawled_urls)
        self.redis.expire(url_count_key, EXPIRE_SECONDS)

        hour = datetime(date_time.year, date_time.month, date_time.day, date_time.hour)
        hour_key = URL_HOUR_COUNT_KEY.format(hour=hour)
        self.redis.incrby(hour_key, num_crawled_urls)
        self.redis.expire(hour_key, EXPIRE_SECONDS)

        user_count_key = USER_COUNT_KEY.format(date=date_time.date)
        self.redis.zincrby(user_count_key, num_crawled_urls, hashed_batch.user_id_hash)
        self.redis.expire(user_count_key, EXPIRE_SECONDS)

        host_key = HOST_COUNT_KEY.format(date=date_time.date)
        for item in hashed_batch.items:
            if item.content is None:
                continue

            host = urlparse(item.url).netloc
            self.redis.zincrby(host_key, 1, host)
        self.redis.expire(host_key, EXPIRE_SECONDS)

    def get_stats(self):
        pass


def get_test_batches():
    for path in glob("./devdata/batches/**/*.json.gz", recursive=True):
        print("Processing path", path)
        with gzip.open(path) as gzip_file:
            yield HashedBatch.parse_raw(gzip_file.read())


if __name__ == '__main__':
    redis = Redis(host='localhost', port=6379, decode_responses=True)
    stats = StatsManager(redis)
    batches = get_test_batches()
    start = datetime.now()
    processed = 0
    for batch in islice(batches, 100):
        stats.record_batch(batch)
        processed += 1
    total_time = (datetime.now() - start).total_seconds()
    print("Processed", processed)
    print("Total time", total_time)
    print("Time per batch", total_time/processed)
