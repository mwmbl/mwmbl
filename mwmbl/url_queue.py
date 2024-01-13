import time
from collections import defaultdict
from datetime import datetime, timedelta
from logging import getLogger
from multiprocessing import Queue
from queue import Empty
from random import Random
from typing import KeysView, Union

from mwmbl.crawler.domains import DomainLinkDatabase
from mwmbl.crawler.urls import BATCH_SIZE, URLDatabase, URLStatus, FoundURL, REASSIGN_MIN_HOURS
from mwmbl.database import Database
from mwmbl.hn_top_domains_filtered import DOMAINS as TOP_DOMAINS, DOMAINS
from mwmbl.indexer.blacklist import is_domain_blacklisted, get_blacklist_domains
from mwmbl.settings import CORE_DOMAINS
from mwmbl.utils import batch, get_domain

logger = getLogger(__name__)


MAX_QUEUE_SIZE = 5000

MAX_TOP_URLS = 100000
MAX_OTHER_URLS = 1000
MAX_URLS_PER_CORE_DOMAIN = 1000
MAX_URLS_PER_TOP_DOMAIN = 100
MAX_URLS_PER_OTHER_DOMAIN = 5
MAX_OTHER_DOMAINS = 10000
INITIALIZE_URLS = 10000


random = Random(1)


class URLQueue:
    def __init__(self, new_item_queue: Queue, queued_batches: Queue, min_top_domains: int = 5):
        """
        new_item_queue: each item in the queue is a list of FoundURLs
        queued_batches: each item in the queue is a list of URLs (strings)
        """
        self._new_item_queue = new_item_queue
        self._queued_batches = queued_batches
        self._other_urls = defaultdict(dict)
        self._top_urls = defaultdict(dict)
        self._min_top_domains = min_top_domains
        assert min_top_domains > 0, "Need a minimum greater than 0 to prevent a never-ending loop"

    def update(self):
        blacklist_domains = get_blacklist_domains()
        num_processed = 0
        while True:
            try:
                new_batch = self._new_item_queue.get_nowait()
                num_processed += 1
            except Empty:
                break
            self._process_found_urls(new_batch, blacklist_domains)
        return num_processed

    def _process_found_urls(self, found_urls: list[FoundURL], blacklist_domains: set[str]):
        logger.info(f"Found URLS: {len(found_urls)}")
        logger.info(f"Found: {found_urls[:100]}")
        valid_urls = [found_url for found_url in found_urls if found_url.status == URLStatus.NEW]
        logger.info(f"Valid URLs: {len(valid_urls)}")

        self._sort_urls(valid_urls, blacklist_domains)
        logger.info(f"Queue size: {self.num_queued_batches}")
        while self.num_queued_batches < MAX_QUEUE_SIZE and len(self._top_urls) >= self._min_top_domains:
            total_top_urls = sum(len(urls) for urls in self._top_urls.values())
            logger.info(f"Total top URLs stored: {total_top_urls} for domains {self._top_urls.keys()}")

            total_other_urls = sum(len(urls) for urls in self._other_urls.values())
            logger.info(f"Total other URLs stored: {total_other_urls} for domains {self._other_urls.keys()}")

            self._batch_urls()
            logger.info(f"Queue size after batching: {self.num_queued_batches}")

    def _sort_urls(self, valid_urls: list[FoundURL], blacklist_domains: set[str]):
        with DomainLinkDatabase() as link_db:
            for found_url in valid_urls:
                try:
                    domain = get_domain(found_url.url)
                except ValueError:
                    continue
                if is_domain_blacklisted(domain, blacklist_domains):
                    continue
                if domain in TOP_DOMAINS:
                    self._top_urls[domain][found_url.url] = 1/len(found_url.url)
                else:
                    domain_score = link_db.get_domain_score(domain)
                    if domain_score > 0:
                        logger.info(f"Domain score for {domain}: {domain_score}")
                        self._other_urls[domain][found_url.url] = 1/len(found_url.url)

        logger.info(f"URL store updated: {len(self._top_urls)} top domains, {len(self._other_urls)} other domains")

        _sort_and_limit_urls(self._top_urls, MAX_TOP_URLS)
        _sort_and_limit_urls(self._other_urls, MAX_OTHER_URLS)

        # Keep only the top "other" domains, ranked by the top item for that domain
        top_other_urls = sorted(self._other_urls.items(), key=lambda x: next(iter(x[1].values())), reverse=True)[:MAX_OTHER_DOMAINS]
        self._other_urls = defaultdict(dict, dict(top_other_urls))

    def _batch_urls(self):
        urls = []
        logger.info("Adding core domains")
        _add_urls(CORE_DOMAINS, self._top_urls, urls, MAX_URLS_PER_CORE_DOMAIN)
        logger.info("Adding top domains")
        _add_urls(TOP_DOMAINS.keys() - CORE_DOMAINS, self._top_urls, urls, MAX_URLS_PER_TOP_DOMAIN)
        logger.info("Adding other domains")
        _add_urls(self._other_urls.keys(), self._other_urls, urls, MAX_URLS_PER_OTHER_DOMAIN)
        self._queue_urls(urls)

    def _queue_urls(self, valid_urls: list[str]):
        random.shuffle(valid_urls)
        for url_batch in batch(valid_urls, BATCH_SIZE):
            self._queued_batches.put(url_batch, block=False)

    @property
    def num_queued_batches(self) -> int:
        return self._queued_batches.qsize()

    @property
    def num_top_domains(self) -> int:
        return len(self._top_urls)


def _sort_and_limit_urls(domain_urls: dict[str, dict[str, float]], max_urls: int):
    for domain, urls in domain_urls.items():
        domain_urls[domain] = dict(sorted(urls.items(), key=lambda url_score: url_score[1], reverse=True)[:max_urls])


def _add_urls(domains: Union[set[str], KeysView], domain_urls: dict[str, dict[str, float]], urls: list[str], max_urls: int):
    for domain in list(domains & domain_urls.keys()):
        urls += list(domain_urls[domain].keys())[:max_urls]
        new_domain_urls = list(domain_urls[domain].items())[max_urls:]
        if len(new_domain_urls) > 0:
            domain_urls[domain] = dict(new_domain_urls)
        else:
            del domain_urls[domain]


def update_queue_continuously(new_item_queue: Queue, queued_batches: Queue):
    queue = URLQueue(new_item_queue, queued_batches)
    while True:
        num_processed = queue.update()
        logger.info(f"Queue update, num processed: {num_processed}, queue size: {queue.num_queued_batches}, num top "
                    f"domains: {queue.num_top_domains}")
        if num_processed == 0:
            time.sleep(5)


