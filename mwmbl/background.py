"""
Script that updates data in a background process.
"""
from logging import getLogger
from pathlib import Path
from time import sleep

from mwmbl.indexer import historical, index_batches
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.paths import INDEX_PATH, BATCH_DIR_NAME
from mwmbl.indexer.preprocess import run_preprocessing
from mwmbl.indexer.update_pages import run_update

logger = getLogger(__name__)


def run(data_path: str):
    # historical.run()
    index_path = Path(data_path) / INDEX_PATH
    batch_cache = BatchCache(Path(data_path) / BATCH_DIR_NAME)
    while True:
        try:
            batch_cache.retrieve_batches(1)
        except Exception:
            logger.exception("Error retrieving batches")
        try:
            index_batches.run(batch_cache, index_path)
        except Exception:
            logger.exception("Error indexing batches")
        sleep(10)
