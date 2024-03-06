"""
Script that updates data in a background process.
"""
import logging
import sys
from datetime import datetime
from logging import getLogger, basicConfig
from pathlib import Path
from time import sleep

from django.conf import settings

from mwmbl.indexer import index_batches, historical
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.paths import BATCH_DIR_NAME, INDEX_NAME
from mwmbl.models import OldIndex
from mwmbl.tinysearchengine.copy_index import copy_pages


NUM_PAGES_TO_COPY = 10


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
    logger.info(f"Found {len(old_indexes)} old indexes")

    # Check if all indexes are copied

    for old_index_info in old_indexes:
        if old_index_info.last_page_copied >= old_index_info.num_pages - 1:
            logger.info(f"All pages copied for index {old_index_info.index_path}")
            continue

        logger.info(f"Copying pages from {old_index_info.index_path} to {new_index_path} starting at page {old_index_info.last_page_copied}")
        end_page = copy_pages(old_index_info.index_path, new_index_path, old_index_info.start_page, NUM_PAGES_TO_COPY)

        # Update the start page
        old_index_info.last_page_copied = end_page
        old_index_info.last_copied_time = datetime.utcnow()
        old_index_info.save()

        logger.info(f"Copied pages from {old_index_info.index_path} to {new_index_path} up to page {end_page}")


def copy_indexes_continuously():
    new_index_path = Path(settings.DATA_PATH) / INDEX_NAME
    while True:
        try:
            copy_all_indexes(new_index_path)
        except Exception:
            logger.exception("Error copying pages")
        sleep(10)
