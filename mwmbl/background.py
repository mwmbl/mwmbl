"""
Script that updates data in a background process.
"""
from logging import getLogger
from pathlib import Path
from time import sleep

from mwmbl.indexer import historical
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
            batch_cache.retrieve_batches()
        except Exception:
            logger.exception("Error retrieving batches")
        try:
            run_preprocessing(index_path)
        except Exception:
            logger.exception("Error preprocessing")
        try:
            run_update(index_path)
        except Exception:
            logger.exception("Error running index update")
        sleep(10)
