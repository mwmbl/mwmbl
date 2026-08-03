from multiprocessing import Process, Queue
from pathlib import Path

from django.apps import AppConfig
from django.conf import settings

from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase


def create_index():
    # Imports here to avoid AppRegistryNotReady exception
    from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE
    index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
    try:
        existing_index = TinyIndex(item_factory=Document, index_path=index_path)
        print("======================================")
        print(f"Found existing index at {index_path}")
        print("======================================")
        if existing_index.page_size != PAGE_SIZE or existing_index.num_pages != settings.NUM_PAGES:
            raise ValueError(f"Existing index page sizes ({existing_index.page_size}) or number of pages "
                             f"({existing_index.num_pages}) do not match")
    except FileNotFoundError:
        print("======================================")
        print("Index not found - creating a new index")
        print("======================================")
        TinyIndex.create(item_factory=Document, index_path=index_path, num_pages=settings.NUM_PAGES,
                         page_size=PAGE_SIZE)


def create_index_db():
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.create_tables()


class MwmblConfig(AppConfig):
    name = "mwmbl"
    verbose_name = "Mwmbl Application"

    def ready(self):
        create_index()
        if settings.HAS_DATABASE:
            create_index_db()
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
            from mwmbl.background import sync_search_counts, report_usage_to_polar

            SYNC_TASK = "mwmbl.background.sync_search_counts"
            POLAR_REPORT_TASK = "mwmbl.background.report_usage_to_polar"

            # Sync search counts once per hour (3600 seconds)
            if not Task.objects.filter(task_name=SYNC_TASK).exists():
                sync_search_counts(repeat=3600, repeat_until=None)

            # Report billable usage overage to Polar once per hour (3600 seconds)
            if not Task.objects.filter(task_name=POLAR_REPORT_TASK).exists():
                report_usage_to_polar(repeat=3600, repeat_until=None)

        except Exception:
            # Don't prevent startup if background task scheduling fails. Release
            # the lock so a later-starting worker can retry instead of waiting
            # out the full TTL.
            log.exception("Failed to schedule background tasks")
            cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)
