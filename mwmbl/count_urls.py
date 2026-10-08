"""
Count the unique URLs and domains in the index, and the results it holds.

A URL is stored once for every term it is indexed under, spread over many pages, so the
only way to count distinct URLs is to read every page. The URLs and domains go into Redis
HyperLogLogs, which count distinct members to within about 1% in 12 KB each.

Reading the whole index takes many hours, so it is done a slice at a time: each run of
mwmbl.background.count_index_urls reads pages for a fixed time budget and records how far
it got, and the next run carries on from there. A new scan starts once the previous one
finished INDEX_COUNT_INTERVAL_DAYS ago. Short runs matter for two reasons. The task queue
runs tasks one at a time (BACKGROUND_TASK_RUN_ASYNC is left at its default, False), so a
long run would hold up the hourly tasks behind it. And a task that runs past MAX_RUN_TIME
loses its lock and can be picked up again, so INDEX_COUNT_SECONDS_PER_RUN must stay well
under it. Even so, each batch is recorded with a compare-and-set on the scan's cursor, so
two runs that do overlap never count the same pages twice.
"""

import os
from datetime import date, timedelta
from logging import getLogger
from pathlib import Path
from time import monotonic

from django.conf import settings
from redis import Redis, WatchError

from mwmbl.tinysearchengine.indexer import Document, PageError, TinyIndex
from mwmbl.utils import parse_url, utc_today

INDEX_RESULT_COUNT_KEY = "index-result-count-{date}"
INDEX_DOMAIN_COUNT_KEY = "index-domain-count-{date}"
INDEX_URL_COUNT_KEY = "index-url-count-{date}"
INDEX_DOMAIN_RESULT_COUNT_KEY = "index-domain-result-count-{date}"

# The scan in progress: a hash of the next page to read and the results counted so far,
# and the two HyperLogLogs being filled. They expire if the scan stops being advanced, so
# an abandoned scan does not leave them behind.
INDEX_SCAN_KEY = "index-count-scan"
INDEX_SCAN_URL_HLL_KEY = "index-count-scan-urls"
INDEX_SCAN_DOMAIN_HLL_KEY = "index-count-scan-domains"
INDEX_SCAN_EXPIRE_SECONDS = 60 * 60 * 24 * 2

# The date the last scan finished. It is set only once the counts are published, so losing
# the scan state mid-scan means the next run starts again rather than waiting a week.
INDEX_SCAN_LAST_FINISHED_KEY = "index-count-scan-last-finished"

# Published counts are kept long enough that the 30 day window in get_counts always has an
# earlier count to carry forward into its first days.
LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 60

# The position of the URL in a stored document's tuple - see Document.as_tuple.
URL_INDEX = 1

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
            if not _count_pages(redis, index, next_page, end_page):
                logger.warning(
                    f"The index count scan moved on from page {next_page} under this run; leaving it to the other one."
                )
                return
            next_page = end_page
            if monotonic() >= deadline:
                break

    logger.info(f"Counted URLs in index pages {first_page} to {next_page} of {num_pages}.")

    if next_page >= num_pages:
        _finish_scan(redis)


def _scan_due(redis: Redis) -> bool:
    last_finished = redis.get(INDEX_SCAN_LAST_FINISHED_KEY)
    if last_finished is None:
        return True
    days_since_last_scan = utc_today() - date.fromisoformat(last_finished)
    return days_since_last_scan >= timedelta(days=settings.INDEX_COUNT_INTERVAL_DAYS)


def _start_scan(redis: Redis):
    logger.info("Starting a scan to count the URLs in the index.")
    pipeline = redis.pipeline()
    pipeline.delete(INDEX_SCAN_URL_HLL_KEY, INDEX_SCAN_DOMAIN_HLL_KEY)
    pipeline.hset(INDEX_SCAN_KEY, mapping={"next_page": 0, "num_results": 0})
    pipeline.expire(INDEX_SCAN_KEY, INDEX_SCAN_EXPIRE_SECONDS)
    pipeline.execute()


def _count_pages(redis: Redis, index: TinyIndex, start_page: int, end_page: int) -> bool:
    """Add pages start_page to end_page to the scan, if it is still at start_page.

    Returns False, recording nothing, if the scan has moved on or gone in the meantime.
    """
    urls = set()
    num_results = 0
    for i in range(start_page, end_page):
        try:
            # The raw tuples rather than get_page: only the URL is needed, and building
            # a Document for every item is most of the cost of reading a page.
            page = index._get_page_tuples(i)
        except (PageError, OSError):
            # An unlocked read while the indexer is writing, so most of these are torn
            # rather than damaged. Count the page as empty, as retrieve() does, rather
            # than abandoning the scan - which, for a device error, would fail on the
            # same page every run.
            logger.warning("Could not read index page %d while counting URLs", i, exc_info=True)
            continue
        urls |= {item[URL_INDEX] for item in page}
        num_results += len(page)

    # Each URL is on many pages, so its domain is worked out once per batch, not per page.
    domains = {parse_url(url).netloc for url in urls}

    # One transaction, so the results are never counted twice: either this batch is
    # recorded along with the page it got up to, or neither is and it is read again. The
    # watch on the cursor means a second run reading the same batch records nothing.
    with redis.pipeline() as pipeline:
        try:
            pipeline.watch(INDEX_SCAN_KEY)
            next_page = pipeline.hget(INDEX_SCAN_KEY, "next_page")
            if next_page is None or int(next_page) != start_page:
                return False
            pipeline.multi()
            pipeline.pfadd(INDEX_SCAN_URL_HLL_KEY, *urls)
            pipeline.pfadd(INDEX_SCAN_DOMAIN_HLL_KEY, *domains)
            pipeline.hincrby(INDEX_SCAN_KEY, "num_results", num_results)
            pipeline.hset(INDEX_SCAN_KEY, "next_page", end_page)
            for key in (INDEX_SCAN_KEY, INDEX_SCAN_URL_HLL_KEY, INDEX_SCAN_DOMAIN_HLL_KEY):
                pipeline.expire(key, INDEX_SCAN_EXPIRE_SECONDS)
            pipeline.execute()
        except WatchError:
            return False
    return True


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

    pipeline = redis.pipeline()
    pipeline.set(INDEX_SCAN_LAST_FINISHED_KEY, str(today))
    pipeline.delete(INDEX_SCAN_KEY, INDEX_SCAN_URL_HLL_KEY, INDEX_SCAN_DOMAIN_HLL_KEY)
    pipeline.execute()


def _set_count(key, redis, today, count):
    redis.set(key.format(date=today), count)
    redis.expire(key.format(date=today), LONG_EXPIRE_SECONDS)


def get_counts(redis: Redis | None = None) -> dict[str, dict[str, int]]:
    """The index counts for each of the last 30 days.

    A scan only finishes about once a week, so each day gets the latest count published
    on or before it - the size of the index as last measured on that day.
    """
    redis = redis or get_redis()
    return {
        "urls_in_index_daily": _get_daily_counts(redis, INDEX_URL_COUNT_KEY),
        "domains_in_index_daily": _get_daily_counts(redis, INDEX_DOMAIN_COUNT_KEY),
        "results_in_index_daily": _get_daily_counts(redis, INDEX_RESULT_COUNT_KEY),
    }


def get_domain_result_count(domain: str) -> int:
    redis = get_redis()

    today = utc_today()
    count = redis.zscore(INDEX_DOMAIN_RESULT_COUNT_KEY.format(date=today), domain)
    return 0 if count is None else int(count)


def _get_daily_counts(redis: Redis, key: str, num_days: int = 30) -> dict[str, int]:
    today = utc_today()
    # Look back far enough before the window to find a count to carry into its first days.
    first_day = today - timedelta(days=num_days - 1 + 2 * settings.INDEX_COUNT_INTERVAL_DAYS)
    days = [first_day + timedelta(days=i) for i in range((today - first_day).days + 1)]
    counts = redis.mget([key.format(date=day) for day in days])

    daily_counts = {}
    latest = None
    for day, count in zip(days, counts):
        if count is not None:
            latest = int(count)
        if latest is not None and day > today - timedelta(days=num_days):
            daily_counts[str(day)] = latest
    return daily_counts
