"""
Script that updates data in a background process.

Also contains Django Background Tasks for periodic maintenance:
  - sync_search_counts: syncs Redis monthly counters → DB once per hour
  - report_usage_to_polar: reports billable usage overage to Polar once per hour
  - refresh_blacklist_snapshot: rebuilds the blacklist the search path filters against
  - purge_blacklisted_from_queue: removes retrieval-filtered documents from the index
  - index_wiki_results_from_queue: writes searches' Wikipedia results into the index
"""
import logging
import sys
from collections import defaultdict
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
from mwmbl.indexer.wiki_cache import drain_wiki_queue, unseen_wiki_urls, wiki_queue_size
from mwmbl.models import OldIndex, UsageBucket
from mwmbl.quota import MONTHLY_TTL, _monthly_key, get_all_monthly_keys
from mwmbl.tinysearchengine.copy_index import copy_pages
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.rank import get_wiki_intro_extracts
from mwmbl.utils import batch

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


# ---------------------------------------------------------------------------
# Wikipedia results (Django Background Tasks)
# ---------------------------------------------------------------------------

def _index_wiki_term_copies(documents: list[Document], index_path: str) -> None:
    """Write the queued per-term copies: the cache entry, plus any real query terms."""
    page_documents = defaultdict(list)
    with TinyIndex(Document, index_path, 'r') as index:
        for document in documents:
            page_documents[index.get_key_page_index(document.term)].append(document)

    # Highest score first. Under the query-hash term sort_documents scores every document
    # against a term none of them can match, so they all tie and it keeps whatever order
    # they arrived in - and the queue is a SET drained with SPOP, so that order is
    # arbitrary. Sorting here means a page too full to take all of a query's results keeps
    # Wikipedia's top ones rather than three at random.
    for documents_for_page in page_documents.values():
        documents_for_page.sort(key=lambda document: -(document.score or 0.0))

    index_batches.index_pages(index_path, page_documents)


def _with_intro_extracts(documents: list[Document]) -> list[Document]:
    """The same documents with the query-chosen snippet replaced by the article intro.

    A search result's extract is the snippet Wikipedia picked because it matched the query.
    For a document about to be filed under its own tokens that is doubly wrong: it is
    arbitrary as index content, and tokenize_document tokenizes the extract, so it decides
    which pages the document lands on. Anything we could not fetch keeps its snippet.

    The term is dropped: these are going through preprocess_documents, which files them
    under their own tokens, so a query term has no business travelling with them.
    """
    extracts = {}
    titles = [document.title for document in documents]
    for title_batch in batch(titles, settings.WIKI_INTRO_BATCH_SIZE):
        extracts.update(get_wiki_intro_extracts(title_batch))

    return [Document(title=document.title,
                     url=document.url,
                     extract=extracts.get(document.title, document.extract),
                     score=document.score,
                     state=document.state,
                     last_crawled=document.last_crawled)
            for document in documents]


def _index_unseen_wiki_pages(documents: list[Document], index_path: str) -> int:
    """Send Wikipedia pages we have not seen before through the standard indexing path."""
    if not settings.WIKI_CACHE_GENERAL_INDEX:
        return 0

    unseen = unseen_wiki_urls(documents, settings.WIKI_CACHE_GENERAL_BATCH_SIZE)
    if not unseen:
        return 0

    index_batches.index_documents(_with_intro_extracts(unseen), index_path)
    return len(unseen)


@background(schedule=0)
def index_wiki_results_from_queue():
    """
    Write the Wikipedia results that searches have found into the index.

    Search workers only enqueue. An index page write is a read-modify-write, and doing it
    from every gunicorn worker on a large fraction of searches would have them fighting
    over the same 4 KB pages, so it is serialised here - the same arrangement as the
    blacklist purge.

    Each batch does two things: writes the queued per-term copies (the cache entry under
    the query hash, plus real query terms for the results that passed the anonymisation
    gate), and sends any URL we have not seen before through the standard indexing path so
    the page is discoverable generally and not only for the query that surfaced it.
    """
    documents = drain_wiki_queue(settings.WIKI_CACHE_INDEX_BATCH_SIZE)
    if not documents:
        return

    index_path = str(Path(settings.DATA_PATH) / settings.INDEX_NAME)
    # General indexing first, term copies second. combine_documents dedupes by URL across a
    # whole page, not per term, so when a document's own tokens put it on the same page as
    # one of its term copies, one of the two is dropped - and whichever is written last
    # wins. The term copies are the ones a search looks up by, so they go last: losing one
    # of ~57 general copies costs a little discoverability, while losing the cache entry
    # would mean re-fetching from Wikipedia on every repeat of the query.
    num_indexed = _index_unseen_wiki_pages(documents, index_path)
    _index_wiki_term_copies(documents, index_path)

    logger.info("Wrote %d Wikipedia term copies and generally indexed %d new pages; "
                "%d entries still queued", len(documents), num_indexed, wiki_queue_size())


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
