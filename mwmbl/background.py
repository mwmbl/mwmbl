"""
Script that updates data in a background process.

Also contains Django Background Tasks for periodic maintenance:
  - sync_search_counts: syncs Redis monthly counters → DB once per hour
  - report_usage_to_polar: reports billable usage overage to Polar once per hour
  - refresh_blacklist_snapshot: rebuilds the blacklist the search path filters against
  - purge_blacklisted_from_queue: removes retrieval-filtered documents from the index
"""
import logging
import sys
from datetime import datetime, timezone
from logging import getLogger, basicConfig
from pathlib import Path
from time import sleep

from background_task import background
from django.conf import settings
from django.core.cache import cache
from redis import Redis

from mwmbl import pricing
from mwmbl.crawler.stats import StatsManager
from mwmbl.indexer import index_batches, historical
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist, refresh_snapshot
from mwmbl.indexer.purge_blacklisted import purge_documents
from mwmbl.indexer.purge_queue import drain_purge_queue, queue_size
from mwmbl.models import OldIndex, UsageBucket
from mwmbl.quota import MONTHLY_TTL, _monthly_key, get_all_monthly_keys
from mwmbl.tinysearchengine.copy_index import copy_pages
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

NUM_PAGES_TO_COPY = 1024


basicConfig(stream=sys.stdout, level=logging.INFO)
logger = getLogger(__name__)

stats_manager = StatsManager(Redis.from_url(settings.REDIS_URL, decode_responses=True))


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


# ---------------------------------------------------------------------------
# Blacklisted-domain maintenance (Django Background Tasks)
# ---------------------------------------------------------------------------

@background(schedule=0)
def refresh_blacklist_snapshot():
    """
    Rebuild the blacklist snapshot that the search workers filter against.

    This is the only place the multi-megabyte remote blocklists are downloaded and
    parsed. Search workers only ever read the ~11 MB hash array this publishes to Redis,
    so no query ever waits on a blocklist fetch. See mwmbl.indexer.blacklist_snapshot.
    """
    refresh_snapshot()


@background(schedule=0)
def purge_blacklisted_from_queue():
    """
    Remove the documents that retrieval filtered out from the index itself.

    Retrieval already guarantees these are never shown; this closes the loop so the same
    documents are not re-filtered on every future query. The queue is best-effort, and
    that is fine: anything lost is re-queued the next time a query surfaces it.
    """
    documents = drain_purge_queue(settings.BLACKLIST_PURGE_BATCH_SIZE)
    if not documents:
        return

    blacklist = get_snapshot_blacklist()
    index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    with TinyIndex(Document, str(index_path), 'w') as index:
        removed_by_domain = purge_documents(index, documents, blacklist.is_domain_blacklisted)

    num_removed = sum(removed_by_domain.values())
    stats_manager.record_blacklisted_removed(num_removed)

    logger.info("Purged %d documents from the index across %d domains; %d still queued",
                num_removed, len(removed_by_domain), queue_size())


@background(schedule=0)
def report_usage_to_polar():
    """
    Reports each user's billable overage (requests beyond the free 2,000/month)
    to Polar as usage events, once per hour.

    Only the delta since the last report is sent (UsageBucket.reported_overage
    tracks how much has already been ingested), so re-runs are idempotent and a
    failed batch simply gets resent (larger) on the next run rather than risking
    double-counting.
    """
    from polar_sdk import Polar

    now = datetime.now(timezone.utc)

    events = []
    buckets_to_update = []
    for bucket in UsageBucket.objects.filter(year=now.year, month=now.month).select_related("user__billing"):
        billing = getattr(bucket.user, "billing", None)
        if not billing or not billing.polar_customer_id:
            continue  # no Polar customer yet — nothing to report

        total_overage = pricing.billable_overage(bucket.count)
        delta = total_overage - bucket.reported_overage
        if delta <= 0:
            continue

        events.append({
            "name": "search_request",
            "external_customer_id": str(bucket.user.id),
            "metadata": {"quantity": delta},
        })
        bucket.reported_overage = total_overage
        buckets_to_update.append(bucket)

    if not events:
        return

    try:
        with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
            polar.events.ingest(request={"events": events})
    except Exception:
        logger.exception("Error reporting usage to Polar")
        return

    UsageBucket.objects.bulk_update(buckets_to_update, ["reported_overage"])
