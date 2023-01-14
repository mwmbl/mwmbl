import time
from datetime import datetime, timedelta
from logging import getLogger
from multiprocessing import Queue
from queue import Empty
from time import sleep

from mwmbl.crawler.urls import BATCH_SIZE, URLDatabase, URLStatus, FoundURL, REASSIGN_MIN_HOURS
from mwmbl.database import Database
from mwmbl.utils import batch


logger = getLogger(__name__)


MAX_QUEUE_SIZE = 5000
MIN_QUEUE_SIZE = 1000


class URLQueue:
    def __init__(self, new_item_queue: Queue, queued_batches: Queue):
        """
        new_item_queue: each item in the queue is a list of FoundURLs
        queued_batches: each item in the queue is a list of URLs (strings)
        """
        self._new_item_queue = new_item_queue
        self._queued_batches = queued_batches

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
            self.process_found_urls(new_batch)
        return num_processed

    def process_found_urls(self, found_urls: list[FoundURL]):
        min_updated_date = datetime.utcnow() - timedelta(hours=REASSIGN_MIN_HOURS)

        valid_urls = [found_url.url for found_url in found_urls if found_url.status == URLStatus.NEW or (
                found_url.status == URLStatus.ASSIGNED and found_url.timestamp < min_updated_date)]

        self._queue_urls(valid_urls)

    def _queue_urls(self, valid_urls: list[str]):
        for url_batch in batch(valid_urls, BATCH_SIZE):
            self._queued_batches.put(url_batch, block=False)

    @property
    def num_queued_batches(self):
        return self._queued_batches.qsize()


def update_queue_continuously(new_item_queue: Queue, queued_batches: Queue):
    queue = URLQueue(new_item_queue, queued_batches)
    queue.initialize()
    while True:
        num_processed = queue.update()
        logger.info(f"Queue update, num processed: {num_processed}, queue size: {queue.num_queued_batches}")
        if num_processed == 0:
            time.sleep(5)


