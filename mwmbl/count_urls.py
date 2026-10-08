"""
Count the unique URLs and domains in the index, and the results it holds.

A URL is stored once for every term it is indexed under, spread over many pages, so the
only way to count distinct URLs is to read every page. The URLs and domains go into Redis
HyperLogLogs, which count distinct members to within about 1% in 12 KB each.

Reading the whole index takes many hours, and the background task queue runs one task at
a time, so a single scan would hold up everything else in it. Instead the scan is done a
slice at a time: each run of mwmbl.background.count_index_urls reads pages for a fixed
time budget and records how far it got, and the next run carries on from there. A new
scan starts once the previous one is INDEX_COUNT_INTERVAL_DAYS old.
"""

import os
from datetime import date, timedelta
from logging import getLogger
from pathlib import Path
from time import monotonic

from django.conf import settings
from redis import Redis

from mwmbl.tinysearchengine.indexer import Document, PageError, TinyIndex
from mwmbl.utils import parse_url, utc_today

INDEX_RESULT_COUNT_KEY = "index-result-count-{date}"
INDEX_DOMAIN_COUNT_KEY = "index-domain-count-{date}"
INDEX_URL_COUNT_KEY = "index-url-count-{date}"
INDEX_DOMAIN_RESULT_COUNT_KEY = "index-domain-result-count-{date}"

# The scan in progress: a hash of the next page to read and the results counted so far,
# and the two HyperLogLogs being filled.
INDEX_SCAN_KEY = "index-count-scan"
INDEX_SCAN_URL_HLL_KEY = "index-count-scan-urls"
INDEX_SCAN_DOMAIN_HLL_KEY = "index-count-scan-domains"
INDEX_SCAN_LAST_STARTED_KEY = "index-count-scan-last-started"

LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30

# Pages read between progress updates. The time budget is checked once per batch, and a
# batch is also the most that is read twice if the process dies mid-run.
NUM_PAGES_IN_BATCH = 1024


logger = getLogger(__name__)


def get_redis():
    return Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)


def count_urls_step(redis: Redis, index_path: Path, time_budget_seconds: float):
    """Advance the index scan by up to time_budget_seconds, starting one if it is due.

    When the scan reaches the last page, the counts are published under today's date.
    """
    scan = redis.hgetall(INDEX_SCAN_KEY)
    if scan:
        next_page = int(scan["next_page"])
    elif _scan_due(redis):
        _start_scan(redis)
        next_page = 0
    else:
        return

    deadline = monotonic() + time_budget_seconds
    first_page = next_page
    with TinyIndex(item_factory=Document, index_path=index_path) as index:
        num_pages = index.num_pages
        # The budget is checked after each batch rather than before, so every run reads
        # at least one batch however long the one before it overran.
        while next_page < num_pages:
            end_page = min(next_page + NUM_PAGES_IN_BATCH, num_pages)
            _count_pages(redis, index, next_page, end_page)
            next_page = end_page
            if monotonic() >= deadline:
                break

    logger.info(f"Counted URLs in index pages {first_page} to {next_page} of {num_pages}.")

    if next_page >= num_pages:
        _finish_scan(redis)


def _scan_due(redis: Redis) -> bool:
    last_started = redis.get(INDEX_SCAN_LAST_STARTED_KEY)
    if last_started is None:
        return True
    days_since_last_scan = utc_today() - date.fromisoformat(last_started)
    return days_since_last_scan >= timedelta(days=settings.INDEX_COUNT_INTERVAL_DAYS)


def _start_scan(redis: Redis):
    logger.info("Starting a scan to count the URLs in the index.")
    pipeline = redis.pipeline()
    pipeline.delete(INDEX_SCAN_URL_HLL_KEY, INDEX_SCAN_DOMAIN_HLL_KEY)
    pipeline.hset(INDEX_SCAN_KEY, mapping={"next_page": 0, "num_results": 0})
    pipeline.set(INDEX_SCAN_LAST_STARTED_KEY, str(utc_today()))
    pipeline.execute()


def _count_pages(redis: Redis, index: TinyIndex, start_page: int, end_page: int):
    urls = set()
    domains = set()
    num_results = 0
    for i in range(start_page, end_page):
        try:
            page = index.get_page(i)
        except PageError:
            # An unlocked read while the indexer is writing, so most of these are torn
            # rather than damaged. Count the page as empty, as retrieve() does, rather
            # than abandoning the scan.
            logger.warning("Could not read index page %d while counting URLs", i)
            continue
        urls |= {doc.url for doc in page}
        domains |= {parse_url(doc.url).netloc for doc in page}
        num_results += len(page)

    # One transaction, so the results are never counted twice: either this batch is
    # recorded along with the page it got up to, or neither is and it is read again.
    pipeline = redis.pipeline()
    pipeline.pfadd(INDEX_SCAN_URL_HLL_KEY, *urls)
    pipeline.pfadd(INDEX_SCAN_DOMAIN_HLL_KEY, *domains)
    pipeline.hincrby(INDEX_SCAN_KEY, "num_results", num_results)
    pipeline.hset(INDEX_SCAN_KEY, "next_page", end_page)
    pipeline.execute()


def _finish_scan(redis: Redis):
    url_count = redis.pfcount(INDEX_SCAN_URL_HLL_KEY)
    domain_count = redis.pfcount(INDEX_SCAN_DOMAIN_HLL_KEY)
    num_results = int(redis.hget(INDEX_SCAN_KEY, "num_results"))

    logger.info(
        f"Counted {url_count} unique URLs, {domain_count} unique domains and {num_results} results in the index."
    )

    today = utc_today()
    _set_count(INDEX_URL_COUNT_KEY, redis, today, url_count)
    _set_count(INDEX_DOMAIN_COUNT_KEY, redis, today, domain_count)
    _set_count(INDEX_RESULT_COUNT_KEY, redis, today, num_results)

    redis.delete(INDEX_SCAN_KEY, INDEX_SCAN_URL_HLL_KEY, INDEX_SCAN_DOMAIN_HLL_KEY)


def _set_count(key, redis, today, count):
    redis.set(key.format(date=today), count)
    redis.expire(key.format(date=today), LONG_EXPIRE_SECONDS)


def get_counts() -> dict[str, dict[str, int]]:
    redis = get_redis()

    today = utc_today()

    urls_in_index_daily = {}
    domains_in_index_daily = {}
    results_in_index_daily = {}
    for i in range(29, -1, -1):
        date_i = today - timedelta(days=i)

        _get_count(redis, urls_in_index_daily, INDEX_URL_COUNT_KEY, date_i)
        _get_count(redis, domains_in_index_daily, INDEX_DOMAIN_COUNT_KEY, date_i)
        _get_count(redis, results_in_index_daily, INDEX_RESULT_COUNT_KEY, date_i)

    return {
        "urls_in_index_daily": urls_in_index_daily,
        "domains_in_index_daily": domains_in_index_daily,
        "results_in_index_daily": results_in_index_daily,
    }


def get_domain_result_count(domain: str) -> int:
    redis = get_redis()

    today = utc_today()
    count = redis.zscore(INDEX_DOMAIN_RESULT_COUNT_KEY.format(date=today), domain)
    return 0 if count is None else int(count)


def _get_count(redis, count_dict, key, date_i):
    """
    Get the count for a given date and set it in the count_dict.
    """
    count = redis.get(key.format(date=date_i))
    if count is not None:
        count_dict[str(date_i)] = int(count)
