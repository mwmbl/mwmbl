from collections import Counter
from datetime import datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path
from random import Random
from time import sleep

from django.conf import settings
from pydistinct.stats_estimators import smoothed_jackknife_estimator

from mwmbl.models import DailyDomainResultCount, DailyIndexStats
from mwmbl.tinysearchengine.indexer import Document, PageError, TinyIndex
from mwmbl.utils import parse_url, utc_today

LONG_EXPIRE_SECONDS = 60 * 60 * 24 * 30

PAGE_PROPORTION_TO_SAMPLE = 0.01


logger = getLogger(__name__)
random = Random(1)


def count_urls_continuously():
    while True:
        start_time = datetime.now(timezone.utc)
        count_urls()
        end_time = datetime.now(timezone.utc)
        total_time = end_time - start_time
        time_remaining = 60 * 60 * 24 - total_time.total_seconds()
        logger.info(f"Counting took {total_time}. Sleeping for {timedelta(seconds=time_remaining)}.")
        sleep(time_remaining)


def count_urls():
    start_time = datetime.now(timezone.utc)

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
            try:
                page = index.get_page(i)
            except PageError:
                # An unlocked read while the indexer is writing, so most of these are torn
                # rather than damaged. Count the page as empty, as retrieve() does, rather
                # than abandoning the sample.
                logger.warning("Could not read index page %d while counting URLs", i)
                continue
            url_counts.update({doc.url for doc in page})
            domains = [parse_url(doc.url).netloc for doc in page]
            domain_counts.update(domains)
            total_docs += len(page)

    num_results_estimate = int(total_docs / PAGE_PROPORTION_TO_SAMPLE)
    url_count_estimate = smoothed_jackknife_estimator(attributes=dict(url_counts.items()), n_pop=num_results_estimate)
    domain_count_estimate = smoothed_jackknife_estimator(
        attributes=dict(domain_counts.items()), n_pop=num_results_estimate
    )

    logger.info(
        f"Estimated {url_count_estimate} unique URLs, {domain_count_estimate} unique domains, "
        f"and {num_results_estimate} results in the index."
    )

    today = utc_today()

    # Also persist to Postgres (DailyIndexStats)
    DailyIndexStats.objects.update_or_create(
        date=today,
        defaults={
            "urls_in_index": int(url_count_estimate),
            "domains_in_index": int(domain_count_estimate),
            "results_in_index": num_results_estimate,
        },
    )

    # Persist per-domain result counts to Postgres (DailyDomainResultCount)
    for domain, count in domain_counts.items():
        estimated_count = int(count / PAGE_PROPORTION_TO_SAMPLE)
        DailyDomainResultCount.objects.update_or_create(
            date=today,
            domain=domain,
            defaults={"count": estimated_count},
        )

    end_time = datetime.now(timezone.utc)
    logger.info(f"Counting took {end_time - start_time}.")


def get_counts() -> dict[str, dict[str, int]]:
    today = utc_today()

    urls_in_index_daily = {}
    domains_in_index_daily = {}
    results_in_index_daily = {}

    # Read from Postgres (DailyIndexStats)
    thirty_days_ago = today - timedelta(days=29)
    daily_index_stats = DailyIndexStats.objects.filter(date__gte=thirty_days_ago).order_by("date")

    # Build a lookup dict from Postgres data
    pg_stats_by_date = {str(stat.date): stat for stat in daily_index_stats}

    for i in range(29, -1, -1):
        date_i = today - timedelta(days=i)
        date_str = str(date_i)

        pg_stat = pg_stats_by_date.get(date_str)
        if pg_stat:
            urls_in_index_daily[date_str] = pg_stat.urls_in_index
            domains_in_index_daily[date_str] = pg_stat.domains_in_index
            results_in_index_daily[date_str] = pg_stat.results_in_index
        else:
            urls_in_index_daily[date_str] = 0
            domains_in_index_daily[date_str] = 0
            results_in_index_daily[date_str] = 0

    return {
        "urls_in_index_daily": urls_in_index_daily,
        "domains_in_index_daily": domains_in_index_daily,
        "results_in_index_daily": results_in_index_daily,
    }


def get_domain_result_count(domain: str) -> int:
    today = utc_today()
    stat = DailyDomainResultCount.objects.filter(date=today, domain=domain).first()
    return stat.count if stat else 0


if __name__ == "__main__":
    # configure logging
    import logging

    logging.basicConfig(level=logging.INFO)

    count_urls()
    counts = get_counts()
    print("Counts", counts, sep="\n")

    github_count = get_domain_result_count("github.com")
    print("GitHub count", github_count)
