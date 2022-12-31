"""
Script that updates data in a background process.
"""
from logging import getLogger
from multiprocessing import Queue
from pathlib import Path
from time import sleep

from mwmbl.crawler.urls import URLDatabase
from mwmbl.database import Database
from mwmbl.indexer import index_batches, historical, update_urls
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.paths import BATCH_DIR_NAME, INDEX_NAME
from mwmbl.url_queue import update_url_queue, initialize_url_queue

logger = getLogger(__name__)


def run(data_path: str, url_queue: Queue):
    logger.info("Started background process")

    with Database() as db:
        url_db = URLDatabase(db.connection)
        url_db.create_tables()

    initialize_url_queue(url_queue)
    try:
        update_url_queue(url_queue)
    except Exception:
        logger.exception("Error updating URL queue")
    historical.run()
    index_path = Path(data_path) / INDEX_NAME
    batch_cache = BatchCache(Path(data_path) / BATCH_DIR_NAME)

    while True:
        try:
            update_url_queue(url_queue)
        except Exception:
            logger.exception("Error updating URL queue")
        try:
            batch_cache.retrieve_batches(num_batches=10000)
        except Exception:
            logger.exception("Error retrieving batches")
        try:
            update_urls.run(batch_cache)
        except Exception:
            logger.exception("Error updating URLs")
        try:
            index_batches.run(batch_cache, index_path)
        except Exception:
            logger.exception("Error indexing batches")
        sleep(10)
