import os
from collections import Counter
from datetime import date, timedelta, datetime
from logging import getLogger
from pathlib import Path
from random import Random
from time import sleep

from django.conf import settings
from pydistinct.stats_estimators import smoothed_jackknife_estimator
from redis import Redis

from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.utils import parse_url

INDEX_RESULT_COUNT_KEY = "index-result-count-{date}"
INDEX_DOMAIN_COUNT_KEY = "index-domain-count-{date}"
INDEX_URL_COUNT_KEY = "index-url-count-{date}"
INDEX_DOMAIN_RESULT_COUNT_KEY = "index-domain-result-count-{date}"

LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30

PAGE_PROPORTION_TO_SAMPLE = 0.01


logger = getLogger(__name__)
random = Random(1)


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
    start_time = datetime.utcnow()

    index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    with TinyIndex(item_factory=Document, index_path=index_path) as index:
        page_sample = set()
        num_pages_to_sample = max(100, int(index.num_pages * PAGE_PROPORTION_TO_SAMPLE))
        logger.info(f"Sampling {num_pages_to_sample} pages.")
        while len(page_sample) < num_pages_to_sample:
            page_sample.add(random.randrange(index.num_pages))

        url_counts = Counter()
        domain_counts = Counter()
        total_docs = 0
        for i in page_sample:
            page = index.get_page(i)
            url_counts.update({doc.url for doc in page})
            domains = [parse_url(doc.url).netloc for doc in page]
            domain_counts.update(domains)
            total_docs += len(page)

    num_results_estimate = int(total_docs / PAGE_PROPORTION_TO_SAMPLE)
    url_count_estimate = smoothed_jackknife_estimator(attributes=dict(url_counts.items()), n_pop=num_results_estimate)
    domain_count_estimate = smoothed_jackknife_estimator(attributes=dict(domain_counts.items()), n_pop=num_results_estimate)

    logger.info(f"Estimated {url_count_estimate} unique URLs, {domain_count_estimate} unique domains, "
                f"and {num_results_estimate} results in the index.")

    redis = get_redis()

    today = date.today()
    _set_count(INDEX_URL_COUNT_KEY, redis, today, int(url_count_estimate))
    _set_count(INDEX_DOMAIN_COUNT_KEY, redis, today, int(domain_count_estimate))
    _set_count(INDEX_RESULT_COUNT_KEY, redis, today, num_results_estimate)

    end_time = datetime.utcnow()
    logger.info(f"Counting took {end_time - start_time}.")


def _set_count(key, redis, today, count):
    redis.set(key.format(date=today), count)
    redis.expire(key.format(date=today), LONG_EXPIRE_SECONDS)


def get_counts() -> dict[str, dict[str, int]]:
    redis = get_redis()

    today = date.today()

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

    today = date.today()
    count = redis.zscore(INDEX_DOMAIN_RESULT_COUNT_KEY.format(date=today), domain)
    return 0 if count is None else int(count)


def _get_count(redis, count_dict, key, date_i):
    """
    Get the count for a given date and set it in the count_dict.
    """
    count = redis.get(key.format(date=date_i))
    if count is not None:
        count_dict[str(date_i)] = int(count)


if __name__ == "__main__":
    # configure logging
    import logging
    logging.basicConfig(level=logging.INFO)

    count_urls()
    counts = get_counts()
    print("Counts", counts, sep="\n")

    github_count = get_domain_result_count("github.com")
    print("GitHub count", github_count)
