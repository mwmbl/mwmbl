from multiprocessing import Process, Queue
from pathlib import Path

from django.apps import AppConfig
from django.conf import settings

from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase


def create_index(index_name, num_pages, rebuild_on_mismatch=False):
    """Create the index file if it is missing, and check an existing one is the right shape.

    rebuild_on_mismatch is for indexes whose contents are disposable. The search index is
    not one of them - a size that disagrees with settings there means somebody changed
    NUM_PAGES against a 400 GB file holding the only copy of the crawl, and refusing to
    start is the right answer. The external results cache is the opposite case: resizing it
    should be a config change, not a startup crash, and the cost of rebuilding is
    re-fetching.
    """
    # Imports here to avoid AppRegistryNotReady exception
    from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE
    index_path = Path(settings.DATA_PATH) / index_name
    try:
        existing_index = TinyIndex(item_factory=Document, index_path=index_path)
    except FileNotFoundError:
        print("======================================")
        print("Index not found - creating a new index")
        print("======================================")
        _create_index_file(index_path, num_pages)
        return
    except ValueError as e:
        # A file that is there but does not parse as an index: TinyIndexMetadata.from_bytes
        # rejects it before there is a page size to compare, so the size check below never
        # sees it. For a disposable index that is the same situation as a size mismatch -
        # unusable contents, cheap to rebuild - and crashing the boot on it would be the
        # thing rebuild_on_mismatch exists to avoid.
        if not rebuild_on_mismatch:
            raise
        print(f"{index_path} is not a readable index ({e}) - rebuilding")
        _replace_index_file(index_path, num_pages)
        return

    print("======================================")
    print(f"Found existing index at {index_path}")
    print("======================================")
    if existing_index.page_size == PAGE_SIZE and existing_index.num_pages == num_pages:
        return

    message = (f"Existing index page sizes ({existing_index.page_size}) or number of pages "
               f"({existing_index.num_pages}) do not match")
    if not rebuild_on_mismatch:
        raise ValueError(message)
    print(f"{message} - rebuilding {index_path}")
    _replace_index_file(index_path, num_pages)


def _create_index_file(index_path, num_pages):
    from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE
    TinyIndex.create(item_factory=Document, index_path=index_path, num_pages=num_pages,
                     page_size=PAGE_SIZE)


def _replace_index_file(index_path, num_pages):
    """Build the replacement alongside the old file and rename it into place.

    unlink() and then create() is not one step. It leaves a window with no file there at
    all, and on a deploy where the outgoing and incoming containers share /data the
    outgoing one's open handles go on writing to the unlinked inode while the new file is
    written beside it - two live indexes under one name, one of which nobody will ever read
    again. A rename is atomic: every reader has either the whole old file or the whole new
    one. Same directory, so it is a rename and not a copy.
    """
    temp_path = index_path.parent / f"{index_path.name}.rebuild"
    temp_path.unlink(missing_ok=True)
    _create_index_file(temp_path, num_pages)
    temp_path.replace(index_path)


def create_index_db():
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.create_tables()


class MwmblConfig(AppConfig):
    name = "mwmbl"
    verbose_name = "Mwmbl Application"

    def ready(self):
        create_index(settings.INDEX_NAME, settings.NUM_PAGES)
        # Note this writes num_pages * 4 KB page by page rather than sparsely, so the first
        # boot after a size change pays for the whole file up front - ~17s for the 15 GB
        # production cache at the ~0.9 GB/s measured locally, which is what the healthcheck
        # grace in app.json has to cover.
        create_index(settings.EXTERNAL_CACHE_INDEX_NAME, settings.EXTERNAL_CACHE_NUM_PAGES,
                     rebuild_on_mismatch=True)
        if settings.HAS_DATABASE:
            create_index_db()
            import mwmbl.signals  # noqa: F401 - connects the post_save receivers
            self._schedule_background_tasks()

    # Cache key guarding the scheduling critical section below. Held just long
    # enough to cover a single deploy's worker-startup burst.
    _SCHEDULE_LOCK_KEY = "mwmbl:schedule_background_tasks_lock"
    _SCHEDULE_LOCK_TIMEOUT = 60

    @staticmethod
    def _schedule_background_tasks():
        """
        Schedule periodic background tasks if they are not already queued.
        Uses django-background-tasks; requires `manage.py process_tasks` to be running.
        """
        import asyncio
        import logging
        log = logging.getLogger(__name__)

        # Under bare `uvicorn` (as opposed to gunicorn+uvicorn-workers), apps.ready()
        # runs with an active event loop, so the sync ORM call below raises
        # SynchronousOnlyOperation. The scheduling is idempotent and the production
        # `process_tasks` worker creates the entry too, so skip with a quiet note.
        try:
            asyncio.get_running_loop()
            log.info("Skipping background task scheduling: running in async context.")
            return
        except RuntimeError:
            pass

        # The gunicorn server runs multiple worker processes, each of which calls
        # this method independently on startup. The "does a Task row already
        # exist?" checks below are plain reads with no transaction or unique
        # constraint backing them, so without this lock, workers starting at the
        # same time (every deploy/restart) can race past the check before any of
        # them commits its Task row, each creating its own duplicate periodic
        # task. cache.add() is atomic (same pattern as the rate limiter in
        # mwmbl.quota), so only the first worker to reach this point proceeds;
        # the rest skip immediately, and the lock's TTL simply bounds how long
        # a crashed winner blocks a retry by a later-starting worker.
        from django.core.cache import cache
        if not cache.add(MwmblConfig._SCHEDULE_LOCK_KEY, "1", timeout=MwmblConfig._SCHEDULE_LOCK_TIMEOUT):
            log.info("Skipping background task scheduling: another worker is already handling it.")
            return

        try:
            from background_task.models import Task
            from mwmbl.background import (
                purge_blacklisted_from_queue, refresh_blacklist_snapshot, report_usage_to_polar,
                sync_search_counts,
            )

            SYNC_TASK = "mwmbl.background.sync_search_counts"
            POLAR_REPORT_TASK = "mwmbl.background.report_usage_to_polar"
            BLACKLIST_SNAPSHOT_TASK = "mwmbl.background.refresh_blacklist_snapshot"
            BLACKLIST_PURGE_TASK = "mwmbl.background.purge_blacklisted_from_queue"

            # Sync search counts once per hour (3600 seconds)
            if not Task.objects.filter(task_name=SYNC_TASK).exists():
                sync_search_counts(repeat=3600, repeat_until=None)

            # Report billable usage overage to Polar once per hour (3600 seconds)
            if not Task.objects.filter(task_name=POLAR_REPORT_TASK).exists():
                report_usage_to_polar(repeat=3600, repeat_until=None)

            # Rebuild the blacklist snapshot the search path filters against. schedule=0
            # on the task means the first run happens immediately rather than one full
            # refresh interval after deploy, which matters because until it lands the
            # search workers have only the built-in rules to go on.
            #
            # Only the *repeating* row counts here: approving a domain submission schedules
            # a one-off rebuild under the same task name (mwmbl.signals), and one of those
            # sitting in the queue at deploy time must not suppress the periodic task.
            if not Task.objects.filter(task_name=BLACKLIST_SNAPSHOT_TASK, repeat__gt=0).exists():
                refresh_blacklist_snapshot(
                    repeat=settings.BLACKLIST_SNAPSHOT_REFRESH_SECONDS, repeat_until=None)

            # Drain the queue of blacklisted documents that retrieval has filtered out
            if not Task.objects.filter(task_name=BLACKLIST_PURGE_TASK).exists():
                purge_blacklisted_from_queue(
                    repeat=settings.BLACKLIST_PURGE_INTERVAL_SECONDS, repeat_until=None)

        except Exception:
            # Don't prevent startup if background task scheduling fails
            log.exception("Failed to schedule background tasks")
        finally:
            # Release promptly rather than holding it for the full TTL: by the
            # time we get here the Task rows (if created) are already committed,
            # so a worker that acquires the lock next will see them via the
            # exists() checks above and correctly skip creating duplicates. The
            # TTL above is only a safety net for a process that dies before
            # reaching this point.
            cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)
