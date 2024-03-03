"""
Script that updates data in a background process.
"""
import logging
import sys
from logging import getLogger, basicConfig
from pathlib import Path
from time import sleep

from mwmbl import settings
from mwmbl.indexer import index_batches, historical
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.paths import BATCH_DIR_NAME, INDEX_NAME
from mwmbl.models import OldIndex
from mwmbl.tinysearchengine.copy_index import copy_pages

basicConfig(stream=sys.stdout, level=logging.INFO)
logger = getLogger(__name__)


def run(data_path: str):
    logger.info("Started background process")

    historical.run()
    index_path = Path(data_path) / INDEX_NAME
    batch_cache = BatchCache(Path(data_path) / BATCH_DIR_NAME)

    while True:
        try:
            batch_cache.retrieve_batches(num_batches=10000)
        except Exception:
            logger.exception("Error retrieving batches")
        try:
            index_batches.run(batch_cache, index_path)
        except Exception:
            logger.exception("Error indexing batches")
        sleep(10)


def copy_all_indexes(new_index_path):
    old_indexes = OldIndex.objects.all()
    for old_index_info in old_indexes:
        copy_pages(old_index_info.index_path, new_index_path, old_index_info.start_page, 10)


def copy_indexes_continuously():
    new_index_path = Path(settings.DATA_PATH) / INDEX_NAME
    while True:
        try:
            copy_all_indexes(new_index_path)
        except Exception:
            logger.exception("Error copying pages")
        sleep(10)
