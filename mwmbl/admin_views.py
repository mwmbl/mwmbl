"""Admin-only visibility on the blacklist filtering state that lives in Redis.

Retrieval filtering, the snapshot refresh and the index purge are three processes talking
to each other through Redis keys, and none of that state is reachable from the Django
admin: a snapshot that never publishes and a purge queue that never drains look exactly
like everything working, because retrieval quietly falls back to the built-in rules either
way. This puts the whole loop - the published snapshot, what this worker actually loaded,
the queue waiting to be purged, and the background tasks that maintain both - on one page.

The page is read-only, and deliberately cheap: the snapshot's size comes from STRLEN so a
page load never pulls the ~11 MB blob out of Redis, and the queue is sampled with
SRANDMEMBER so looking at it cannot consume it.
"""
import os
from datetime import datetime, timedelta
from logging import getLogger

from background_task.models import CompletedTask, Task
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from redis import RedisError

from mwmbl.crawler.stats import BLACKLISTED_REMOVED_COUNT_KEY
from mwmbl.curated_domains import get_curated_domains
from mwmbl.indexer import blacklist_snapshot, purge_queue
from mwmbl.indexer.blacklist_snapshot import (
    HASH_DTYPE, SNAPSHOT_KEY, SNAPSHOT_VERSION_KEY, get_snapshot_blacklist,
)
from mwmbl.indexer.purge_queue import MAX_QUEUE_SIZE, PURGE_QUEUE_KEY, peek_purge_queue

logger = getLogger(__name__)


SNAPSHOT_TASK_NAME = "mwmbl.background.refresh_blacklist_snapshot"
PURGE_TASK_NAME = "mwmbl.background.purge_blacklisted_from_queue"

QUEUE_SAMPLE_SIZE = 50
REMOVED_COUNT_DAYS = 14


def _snapshot_status() -> dict:
    """What is published, and what this worker is actually filtering against.

    The two can differ, and the difference is the interesting part. Each gunicorn worker
    loads the snapshot independently and only re-checks every
    BLACKLIST_SNAPSHOT_CHECK_SECONDS, so a freshly published snapshot legitimately shows
    as out of date here for a few minutes. A worker stuck on an old version for longer
    than that is a real problem.
    """
    client = blacklist_snapshot.get_redis()
    published_version = client.get(SNAPSHOT_VERSION_KEY)
    if published_version is not None:
        published_version = published_version.decode()

    # STRLEN, not GET: the blob is ~11 MB and this page is only reporting on its size.
    # Missing keys have length 0, which is also how the blob's absence is detected.
    blob_size = client.strlen(SNAPSHOT_KEY)

    snapshot = get_snapshot_blacklist()
    return {
        "published_version": published_version,
        "blob_present": blob_size > 0,
        "blob_size": blob_size,
        "blob_domains": blob_size // HASH_DTYPE.itemsize,
        # load_now() refuses a blob that is not a whole number of hashes, so surface the
        # same check rather than leaving a rejected snapshot looking healthy here.
        "blob_size_valid": blob_size % HASH_DTYPE.itemsize == 0,
        "loaded_version": snapshot.loaded_version,
        "loaded_domains": snapshot.num_domains,
        "up_to_date": snapshot.loaded_version == published_version,
        "check_seconds": settings.BLACKLIST_SNAPSHOT_CHECK_SECONDS,
        "refresh_seconds": settings.BLACKLIST_SNAPSHOT_REFRESH_SECONDS,
    }


def _queue_status() -> dict:
    client = purge_queue.get_redis()
    size = client.scard(PURGE_QUEUE_KEY)
    return {
        "size": size,
        "max_size": MAX_QUEUE_SIZE,
        "full": size >= MAX_QUEUE_SIZE,
        "batch_size": settings.BLACKLIST_PURGE_BATCH_SIZE,
        "interval_seconds": settings.BLACKLIST_PURGE_INTERVAL_SECONDS,
        "sample": peek_purge_queue(QUEUE_SAMPLE_SIZE, client),
    }


def _removed_counts(days: int) -> list[dict]:
    """Documents the purge has removed from the index, per day, most recent first.

    Retrieval filters blacklisted domains out of results whether or not the removal ever
    happens, so a queue that is filling while this stays at zero is the signature of a
    broken loop - see StatsManager.record_blacklisted_removed.
    """
    client = purge_queue.get_redis()
    today = datetime.utcnow().date()
    dates = [today - timedelta(days=i) for i in range(days)]
    counts = client.mget([BLACKLISTED_REMOVED_COUNT_KEY.format(date=date) for date in dates])
    return [{"date": date, "count": int(count) if count else 0} for date, count in zip(dates, counts)]


def _curated_status() -> dict:
    """Approved domains, and whether any of them are still being filtered out.

    Curated domains are subtracted from the remote lists when the snapshot is built, so
    "still blacklisted" is normally zero. Anything listed here is either waiting for the
    next rebuild, or blocked by a local rule curation does not override - EXCLUDED_DOMAINS
    or DOMAIN_BLACKLIST_REGEX - which is otherwise invisible.
    """
    curated_domains = get_curated_domains()
    still_blacklisted = get_snapshot_blacklist().filter_blacklisted(curated_domains)
    return {
        "count": len(curated_domains),
        "still_blacklisted": sorted(still_blacklisted),
        "approval_delay_seconds": settings.BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS,
    }


def _task_status() -> list[dict]:
    """The two background tasks that maintain the state above.

    Nothing in this loop runs without a `manage.py process_tasks` worker, and one running
    an image that predates these tasks skips them silently by name rather than failing -
    so "pending, run_at long past, never completed" is what a missing or stale worker
    looks like, and it is otherwise invisible.
    """
    task_names = [SNAPSHOT_TASK_NAME, PURGE_TASK_NAME]
    pending = {task.task_name: task for task in Task.objects.filter(task_name__in=task_names)}

    statuses = []
    for task_name in task_names:
        last_completed = CompletedTask.objects.filter(task_name=task_name).order_by("-run_at").first()
        statuses.append({
            "name": task_name,
            "task": pending.get(task_name),
            "last_completed": last_completed,
        })
    return statuses


@staff_member_required
def blacklist_status_view(request):
    context = {
        "title": "Blacklist status",
        # This page reports on one gunicorn worker's in-memory snapshot, and the request
        # lands on whichever worker the load balancer picked. Named so the template can
        # say which one, because "loaded version" is otherwise ambiguous across workers.
        "worker_pid": os.getpid(),
    }

    # A status page for Redis state has to render when Redis is down - that is precisely
    # the failure it exists to report. Everything below this line comes from Redis, so one
    # handler covers the lot.
    try:
        context["snapshot"] = _snapshot_status()
        context["curated"] = _curated_status()
        context["queue"] = _queue_status()
        context["removed_counts"] = _removed_counts(REMOVED_COUNT_DAYS)
    except RedisError as e:
        logger.exception("Could not read blacklist status from Redis")
        context["redis_error"] = str(e)

    context["tasks"] = _task_status()
    return render(request, "admin/blacklist_status.html", context)
