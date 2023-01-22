import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging import getLogger
from multiprocessing import Queue
from queue import Empty
from typing import KeysView, Union

from mwmbl.crawler.urls import BATCH_SIZE, URLDatabase, URLStatus, FoundURL, REASSIGN_MIN_HOURS
from mwmbl.database import Database
from mwmbl.hn_top_domains_filtered import DOMAINS as TOP_DOMAINS
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


@dataclass
class URLScore:
    url: str
    score: float


class URLQueue:
    def __init__(self, new_item_queue: Queue, queued_batches: Queue):
        """
        new_item_queue: each item in the queue is a list of FoundURLs
        queued_batches: each item in the queue is a list of URLs (strings)
        """
        self._new_item_queue = new_item_queue
        self._queued_batches = queued_batches
        self._other_urls = defaultdict(list)
        self._top_urls = defaultdict(list)

    def initialize(self):
        with Database() as db:
            url_db = URLDatabase(db.connection)
            urls = url_db.get_urls(URLStatus.QUEUED, MAX_QUEUE_SIZE * BATCH_SIZE)
            self._queue_urls(urls)
            logger.info(f"Initialized URL queue with {len(urls)} urls, current queue size: {self.num_queued_batches}")

    def update(self):
        num_processed = 0
        while True:
            try:
                new_batch = self._new_item_queue.get_nowait()
                num_processed += 1
            except Empty:
                break
            self._process_found_urls(new_batch)
        return num_processed

    def _process_found_urls(self, found_urls: list[FoundURL]):
        logger.info(f"Processing found URLs: {found_urls[:1000]}")
        # with open(Path(os.environ["HOME"]) / "data" / "mwmbl" / "found-urls.pickle", "wb") as output_file:
        #     pickle.dump(found_urls, output_file)
        # logger.info("Dumped")

        min_updated_date = datetime.utcnow() - timedelta(hours=REASSIGN_MIN_HOURS)

        logger.info(f"Found URLS: {len(found_urls)}")
        valid_urls = [found_url for found_url in found_urls if found_url.status == URLStatus.NEW.value or (
                found_url.status == URLStatus.ASSIGNED.value and found_url.timestamp < min_updated_date)]
        logger.info(f"Valid URLs: {len(valid_urls)}")

        self._sort_urls(valid_urls)
        logger.info(f"Queue size: {self.num_queued_batches}")
        while self.num_queued_batches < MAX_QUEUE_SIZE and len(self._top_urls) > 0:
            total_top_urls = sum(len(urls) for urls in self._top_urls.values())
            logger.info(f"Total top URLs stored: {total_top_urls}")

            total_other_urls = sum(len(urls) for urls in self._other_urls.values())
            logger.info(f"Total other URLs stored: {total_other_urls}")

            self._batch_urls()
            logger.info(f"Queue size after batching: {self.num_queued_batches}")

    def _sort_urls(self, valid_urls: list[FoundURL]):
        for found_url in valid_urls:
            domain = get_domain(found_url.url)
            url_store = self._top_urls if domain in TOP_DOMAINS else self._other_urls
            url_store[domain].append(URLScore(found_url.url, found_url.score))

        logger.info(f"URL store updated: {len(self._top_urls)} top domains, {len(self._other_urls)} other domains")

        _sort_and_limit_urls(self._top_urls, MAX_TOP_URLS)
        _sort_and_limit_urls(self._other_urls, MAX_OTHER_URLS)

        # Keep only the top "other" domains, ranked by the top item for that domain
        top_other_urls = sorted(self._other_urls.items(), key=lambda x: x[1][0].score, reverse=True)[:MAX_OTHER_DOMAINS]
        self._other_urls = dict(top_other_urls)

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
        for url_batch in batch(valid_urls, BATCH_SIZE):
            self._queued_batches.put(url_batch, block=False)

    @property
    def num_queued_batches(self) -> int:
        return self._queued_batches.qsize()

    @property
    def num_top_domains(self) -> int:
        return len(self._top_urls)


def _sort_and_limit_urls(domain_urls: dict[str, list[str]], max_urls: int):
    for domain, urls in domain_urls.items():
        domain_urls[domain] = sorted(urls, key=lambda url_score: url_score.score, reverse=True)[:max_urls]


def _add_urls(domains: Union[set[str], KeysView], domain_urls: dict[str, list[URLScore]], urls: list[str], max_urls: int):
    for domain in list(domains & domain_urls.keys()):
        new_urls = domain_urls[domain][:max_urls]
        urls += [url_score.url for url_score in new_urls]
        new_domain_urls = domain_urls[domain][max_urls:]
        if len(new_domain_urls) > 0:
            domain_urls[domain] = new_domain_urls
        else:
            del domain_urls[domain]


def update_queue_continuously(new_item_queue: Queue, queued_batches: Queue):
    queue = URLQueue(new_item_queue, queued_batches)
    queue.initialize()
    while True:
        num_processed = queue.update()
        logger.info(f"Queue update, num processed: {num_processed}, queue size: {queue.num_queued_batches}, num top "
                    f"domains: {queue.num_top_domains}")
        if num_processed == 0:
            time.sleep(5)


