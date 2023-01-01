from collections import defaultdict
from datetime import datetime, timezone, timedelta
from logging import getLogger
from typing import Iterable, Collection
from urllib.parse import urlparse

from mwmbl.crawler.batch import HashedBatch
from mwmbl.crawler.urls import URLDatabase, URLStatus, FoundURL
from mwmbl.database import Database
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer import process_batch
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.index_batches import get_url_error_status
from mwmbl.indexer.indexdb import BatchStatus
from mwmbl.settings import UNKNOWN_DOMAIN_MULTIPLIER, EXCLUDED_DOMAINS, SCORE_FOR_SAME_DOMAIN, \
    SCORE_FOR_DIFFERENT_DOMAIN, SCORE_FOR_ROOT_PATH, EXTRA_LINK_MULTIPLIER

logger = getLogger(__name__)


def run(batch_cache: BatchCache):
    process_batch.run(batch_cache, BatchStatus.LOCAL, BatchStatus.URLS_UPDATED, process=record_urls_in_database)


def record_urls_in_database(batches: Collection[HashedBatch]):
    logger.info(f"Recording URLs in database for {len(batches)} batches")
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
                    crawled_page_domain = urlparse(item.url).netloc
                    score_multiplier = 1 if crawled_page_domain in DOMAINS else UNKNOWN_DOMAIN_MULTIPLIER
                    for link in item.content.links:
                        process_link(batch, crawled_page_domain, link, score_multiplier, timestamp, url_scores,
                                     url_timestamps, url_users, False)

                    if item.content.extra_links:
                        for link in item.content.extra_links:
                            process_link(batch, crawled_page_domain, link, score_multiplier, timestamp, url_scores,
                                         url_timestamps, url_users, True)

        found_urls = [FoundURL(url, url_users[url], url_scores[url], url_statuses[url], url_timestamps[url])
                      for url in url_scores.keys() | url_statuses.keys()]

        url_db.update_found_urls(found_urls)


def process_link(batch, crawled_page_domain, link, unknown_domain_multiplier, timestamp, url_scores, url_timestamps, url_users, is_extra: bool):
    parsed_link = urlparse(link)
    if parsed_link.netloc in EXCLUDED_DOMAINS:
        return

    extra_multiplier = EXTRA_LINK_MULTIPLIER if is_extra else 1.0
    score = SCORE_FOR_SAME_DOMAIN if parsed_link.netloc == crawled_page_domain else SCORE_FOR_DIFFERENT_DOMAIN
    url_scores[link] += score * unknown_domain_multiplier * extra_multiplier
    url_users[link] = batch.user_id_hash
    url_timestamps[link] = timestamp
    domain = f'{parsed_link.scheme}://{parsed_link.netloc}/'
    url_scores[domain] += SCORE_FOR_ROOT_PATH * unknown_domain_multiplier
    url_users[domain] = batch.user_id_hash
    url_timestamps[domain] = timestamp


def get_datetime_from_timestamp(timestamp: float) -> datetime:
    batch_datetime = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=timestamp)
    return batch_datetime
