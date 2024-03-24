import os
from datetime import date, timedelta, datetime
from logging import getLogger
from pathlib import Path
from time import sleep
from urllib.parse import urlparse

from django.conf import settings
from redis import Redis

from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.utils import batch

INDEX_RESULT_COUNT_KEY = "index-result-count-{date}"
INDEX_DOMAIN_COUNT_KEY = "index-domain-count-{date}"
INDEX_URL_COUNT_KEY = "index-url-count-{date}"

INDEX_URL_HLL_KEY = "index-hll-{date}"
INDEX_DOMAIN_HLL_KEY = "index-domain-hll-{date}"
SHORT_EXPIRE_SECONDS = 60 * 60 * 24
LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30
NUM_PAGES_IN_BATCH = 1024


logger = getLogger(__name__)


def get_redis():
    return Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)


def count_urls_continuously():
    while True:
        start_time = datetime.utcnow()
        count_urls()
        end_time = datetime.utcnow()
        total_time = (end_time - start_time)
        time_remaining = 60 * 60 * 24 - total_time.total_seconds()
        logger.info(f"Counting took {total_time}. Sleeping for {timedelta(seconds=time_remaining)}.")
        sleep(time_remaining)


def count_urls():
    index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    redis = get_redis()

    logger.info(f"Counting URLs in index {index_path}")

    today = date.today()
    url_hll_key = INDEX_URL_HLL_KEY.format(date=today)
    domain_hll_key = INDEX_DOMAIN_HLL_KEY.format(date=today)
    num_results = 0
    with TinyIndex(item_factory=Document, index_path=index_path) as tiny_index:
        # Count using a Redis hyperloglog to avoid memory issues.
        for page_indexes in batch(range(tiny_index.num_pages), NUM_PAGES_IN_BATCH):
            urls = set()
            domains = set()
            for i in page_indexes:
                docs = tiny_index.get_page(i)
                urls |= {doc.url for doc in docs}
                domains |= {urlparse(doc.url).netloc for doc in docs}
                num_results += len(docs)
            redis.pfadd(url_hll_key, *urls)
            redis.pfadd(domain_hll_key, *domains)
            logger.info(f"Counted {i} pages of {tiny_index.num_pages}.")

    redis.expire(url_hll_key, SHORT_EXPIRE_SECONDS)
    url_count = redis.pfcount(url_hll_key)
    logger.info("Counted %d unique URLs", url_count)

    redis.expire(domain_hll_key, SHORT_EXPIRE_SECONDS)
    domain_count = redis.pfcount(domain_hll_key)
    logger.info("Counted %d unique domains", domain_count)

    _set_count(INDEX_URL_COUNT_KEY, redis, today, url_count)
    _set_count(INDEX_DOMAIN_COUNT_KEY, redis, today, domain_count)
    _set_count(INDEX_RESULT_COUNT_KEY, redis, today, num_results)


def _set_count(key, redis, today, count):
    redis.set(key.format(date=today), count)
    redis.expire(key.format(date=today), LONG_EXPIRE_SECONDS)


def get_counts() -> dict[str, list[int]]:
    redis = get_redis()

    today = date.today()

    urls_in_index_daily = []
    domains_in_index_daily = []
    results_in_index_daily = []
    for i in range(29, -1, -1):
        date_i = today - timedelta(days=i)

        key = INDEX_URL_COUNT_KEY
        urls_in_index_daily.append(_get_count(redis, key, date_i))
        domains_in_index_daily.append(_get_count(redis, INDEX_DOMAIN_COUNT_KEY, date_i))
        results_in_index_daily.append(_get_count(redis, INDEX_RESULT_COUNT_KEY, date_i))

    return {
        "urls_in_index_daily": urls_in_index_daily,
        "domains_in_index_daily": domains_in_index_daily,
        "results_in_index_daily": results_in_index_daily,
    }


def _get_count(redis, key, date_i):
    c = int(redis.get(key.format(date=date_i)) or 0)
    return c


if __name__ == "__main__":
    # configure logging
    import logging
    logging.basicConfig(level=logging.INFO)

    count_urls()
    counts = get_counts()
    print("Counts", counts, sep="\n")
