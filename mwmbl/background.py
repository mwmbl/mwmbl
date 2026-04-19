"""
Script that updates data in a background process.

Also contains Django Background Tasks for periodic quota maintenance:
  - sync_search_counts: syncs Redis monthly counters → DB once per hour
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
def sync_search_counts():
    """
    Bidirectional sync between Redis and UsageBucket, run once per hour.

    Step 1 (Postgres → Redis): seed any missing Redis keys from UsageBucket.
    This restores counters after a Redis restart without persistence.

    Step 2 (Redis → Postgres): update UsageBucket with the live Redis counts
    so Postgres stays current as a durable backup.
    """
    from datetime import datetime, timezone

    from django.core.cache import cache

    from mwmbl.models import UsageBucket
    from mwmbl.quota import MONTHLY_TTL, _monthly_key, get_all_monthly_keys

    now = datetime.now(timezone.utc)

    # Step 1: seed Redis from Postgres, taking the max of the two values.
    # Postgres may lag behind (up to one sync interval), so if Redis already has
    # a higher count we keep it. If Redis was cleared (restart), the Postgres
    # value restores the baseline; any requests made since the restart are
    # already counted in Redis and will be included via max().
    for bucket in UsageBucket.objects.filter(year=now.year, month=now.month):
        key = _monthly_key(bucket.user_id, year=now.year, month=now.month)
        if not cache.add(key, bucket.count, timeout=MONTHLY_TTL):
            # Key already exists — only update if the Postgres value is higher
            current = cache.get(key, default=0)
            if bucket.count > current:
                cache.set(key, bucket.count, timeout=MONTHLY_TTL)

    # Step 2: sync live Redis counters back to Postgres
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
            logger.exception("Error syncing search count for key %s", key)


