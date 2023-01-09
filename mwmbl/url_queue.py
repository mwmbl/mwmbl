from logging import getLogger
from multiprocessing import Queue
from time import sleep

from mwmbl.crawler.urls import BATCH_SIZE, URLDatabase, URLStatus
from mwmbl.database import Database
from mwmbl.utils import batch


logger = getLogger(__name__)


MAX_QUEUE_SIZE = 5000
MIN_QUEUE_SIZE = 1000


def run(url_queue: Queue):
    initialize_url_queue(url_queue)
    while True:
        update_url_queue(url_queue)


def update_url_queue(url_queue: Queue):
    logger.info("Updating URL queue")
    current_size = url_queue.qsize()
    if current_size >= MIN_QUEUE_SIZE:
        logger.info(f"Skipping queue update, current size {current_size}, sleeping for 10 seconds")
        sleep(10)
        return

    with Database() as db:
        url_db = URLDatabase(db.connection)
        urls = url_db.get_urls_for_crawling()
        queue_batches(url_queue, urls)
        logger.info(f"Queued {len(urls)} urls, current queue size: {url_queue.qsize()}")


def initialize_url_queue(url_queue: Queue):
    with Database() as db:
        url_db = URLDatabase(db.connection)
        urls = url_db.get_urls(URLStatus.QUEUED, MAX_QUEUE_SIZE * BATCH_SIZE)
        queue_batches(url_queue, urls)
        logger.info(f"Initialized URL queue with {len(urls)} urls, current queue size: {url_queue.qsize()}")


def queue_batches(url_queue, urls):
    for url_batch in batch(urls, BATCH_SIZE):
        url_queue.put(url_batch, block=False)
