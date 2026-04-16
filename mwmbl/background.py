"""
Script that updates data in a background process.

Also contains Django Background Tasks for periodic quota maintenance:
  - flush_search_counts: syncs Redis monthly counters → DB every 10 minutes
"""
import logging
import sys
from logging import getLogger, basicConfig
from pathlib import Path
from time import sleep

from background_task import background
from django.conf import settings

from mwmbl.indexer import index_batches, historical
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.models import OldIndex
from mwmbl.tinysearchengine.copy_index import copy_pages

NUM_PAGES_TO_COPY = 1024


basicConfig(stream=sys.stdout, level=logging.INFO)
logger = getLogger(__name__)


def run(data_path: str):
    logger.info("Started background process")

    historical.run()
    index_path = Path(data_path) / settings.INDEX_NAME
    batch_cache = BatchCache(Path(data_path) / settings.BATCH_DIR_NAME)

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

    num_updated = 0
    for old_index_info in old_indexes:
        start_page = old_index_info.last_page_copied + 1 if old_index_info.last_page_copied else 0
        end_page = copy_pages(old_index_info.index_path, new_index_path, start_page, NUM_PAGES_TO_COPY)

        if start_page == end_page:
            continue

        # Update the start page
        old_index_info.last_page_copied = end_page
        old_index_info.last_copied_time = datetime.utcnow()
        old_index_info.save()

        logger.info(f"Copied pages from {old_index_info.index_path} to {new_index_path} up to page {end_page}")
        num_updated += 1
    return num_updated


def copy_indexes_continuously():
    new_index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    while True:
        num_updated = 0
        try:
            num_updated = copy_all_indexes(new_index_path)
        except Exception:
            logger.exception("Error copying pages")

        if num_updated == 0:
            sleep(10)


# ---------------------------------------------------------------------------
# Periodic quota maintenance tasks (Django Background Tasks)
# ---------------------------------------------------------------------------

@background(schedule=0)
def flush_search_counts():
    """
    Sync live Redis monthly search counters to UsageBucket records in the
    database.  Scheduled to run every 10 minutes (600 seconds) via
    AppConfig.ready().
    """
    from django.core.cache import cache
    from mwmbl.models import UsageBucket
    from mwmbl.quota import get_all_monthly_keys

    for key in get_all_monthly_keys():
        # key format: search:monthly:{user_id}:{year}:{month}
        try:
            parts = key.split(":")
            user_id = int(parts[2])
            year = int(parts[3])
            month = int(parts[4])
            count = cache.get(key, default=0)
            UsageBucket.objects.update_or_create(
                user_id=user_id, year=year, month=month,
                defaults={"count": count},
            )
        except Exception:
            logger.exception("Error flushing search count for key %s", key)


