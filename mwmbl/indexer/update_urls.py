from collections import defaultdict
from datetime import datetime, timezone, timedelta
from logging import getLogger
from multiprocessing import Queue
from pathlib import Path
from time import sleep
from typing import Collection
from urllib.parse import urlparse

from mwmbl.crawler.batch import HashedBatch
from mwmbl.crawler.urls import URLDatabase, URLStatus, FoundURL
from mwmbl.database import Database
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer import process_batch
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.blacklist import get_blacklist_domains, is_domain_blacklisted
from mwmbl.indexer.index_batches import get_url_error_status
from mwmbl.indexer.indexdb import BatchStatus
from mwmbl.indexer.paths import BATCH_DIR_NAME
from mwmbl.settings import UNKNOWN_DOMAIN_MULTIPLIER, SCORE_FOR_SAME_DOMAIN, \
    SCORE_FOR_DIFFERENT_DOMAIN, SCORE_FOR_ROOT_PATH, EXTRA_LINK_MULTIPLIER
from mwmbl.utils import get_domain

logger = getLogger(__name__)


def update_urls_continuously(data_path: str, new_item_queue: Queue):
    batch_cache = BatchCache(Path(data_path) / BATCH_DIR_NAME)
    while True:
        try:
            run(batch_cache, new_item_queue)
        except Exception:
            logger.exception("Error updating URLs")
        sleep(10)


def run(batch_cache: BatchCache, new_item_queue: Queue):
    process_batch.run(batch_cache, BatchStatus.LOCAL, BatchStatus.URLS_UPDATED, record_urls_in_database, new_item_queue)


def record_urls_in_database(batches: Collection[HashedBatch], new_item_queue: Queue):
    start = datetime.now()
    blacklist_domains = get_blacklist_domains()
    blacklist_retrieval_time = datetime.now() - start
    logger.info(f"Recording URLs in database for {len(batches)} batches, with {len(blacklist_domains)} blacklist "
                f"domains, retrieved in {blacklist_retrieval_time.total_seconds()} seconds")
    with Database() as db:
        url_db = URLDatabase(db.connection)
        url_scores = defaultdict(float)
        url_users = {}
        url_timestamps = {}
        url_statuses = defaultdict(lambda: URLStatus.NEW)
        for batch in batches:
            for item in batch.items:
                timestamp = get_datetime_from_timestamp(item.timestamp / 1000.0)
                url_timestamps[item.url] = timestamp
                url_users[item.url] = batch.user_id_hash
                if item.content is None:
                    url_statuses[item.url] = get_url_error_status(item)
                else:
                    url_statuses[item.url] = URLStatus.CRAWLED
                    try:
                        crawled_page_domain = get_domain(item.url)
                    except ValueError:
                        logger.info(f"Couldn't parse URL {item.url}")
                        continue
                    score_multiplier = 1 if crawled_page_domain in DOMAINS else UNKNOWN_DOMAIN_MULTIPLIER
                    for link in item.content.links:
                        process_link(batch.user_id_hash, crawled_page_domain, link, score_multiplier, timestamp, url_scores,
                                     url_timestamps, url_users, False, blacklist_domains)

                    if item.content.extra_links:
                        for link in item.content.extra_links:
                            process_link(batch.user_id_hash, crawled_page_domain, link, score_multiplier, timestamp, url_scores,
                                         url_timestamps, url_users, True, blacklist_domains)

        found_urls = [FoundURL(url, url_users[url], url_scores[url], url_statuses[url], url_timestamps[url])
                      for url in url_scores.keys() | url_statuses.keys()]

        logger.info(f"Found URLs, {len(found_urls)}")
        urls = url_db.update_found_urls(found_urls)
        new_item_queue.put(urls)
        logger.info(f"Put {len(urls)} new items in the URL queue")


def process_link(user_id_hash, crawled_page_domain, link, unknown_domain_multiplier, timestamp, url_scores, url_timestamps, url_users, is_extra: bool, blacklist_domains):
    parsed_link = urlparse(link)
    if is_domain_blacklisted(parsed_link.netloc, blacklist_domains):
        logger.debug(f"Excluding link for blacklisted domain: {parsed_link}")
        return

    extra_multiplier = EXTRA_LINK_MULTIPLIER if is_extra else 1.0
    score = SCORE_FOR_SAME_DOMAIN if parsed_link.netloc == crawled_page_domain else SCORE_FOR_DIFFERENT_DOMAIN
    url_scores[link] += score * unknown_domain_multiplier * extra_multiplier
    url_users[link] = user_id_hash
    url_timestamps[link] = timestamp
    domain = f'{parsed_link.scheme}://{parsed_link.netloc}/'
    url_scores[domain] += SCORE_FOR_ROOT_PATH * unknown_domain_multiplier
    url_users[domain] = user_id_hash
    url_timestamps[domain] = timestamp


def get_datetime_from_timestamp(timestamp: float) -> datetime:
    batch_datetime = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=timestamp)
    return batch_datetime
