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

        try:
            from background_task.models import Task
            from mwmbl.background import (
                purge_blacklisted_from_queue, refresh_blacklist_snapshot, sync_search_counts,
            )

            SYNC_TASK = "mwmbl.background.sync_search_counts"
            BLACKLIST_SNAPSHOT_TASK = "mwmbl.background.refresh_blacklist_snapshot"
            BLACKLIST_PURGE_TASK = "mwmbl.background.purge_blacklisted_from_queue"

            # Sync search counts once per hour (3600 seconds)
            if not Task.objects.filter(task_name=SYNC_TASK).exists():
                sync_search_counts(repeat=3600, repeat_until=None)

            # Rebuild the blacklist snapshot the search path filters against. schedule=0
            # on the task means the first run happens immediately rather than one full
            # refresh interval after deploy, which matters because until it lands the
            # search workers have only the built-in rules to go on.
            if not Task.objects.filter(task_name=BLACKLIST_SNAPSHOT_TASK).exists():
                refresh_blacklist_snapshot(
                    repeat=settings.BLACKLIST_SNAPSHOT_REFRESH_SECONDS, repeat_until=None)

            # Drain the queue of blacklisted documents that retrieval has filtered out
            if not Task.objects.filter(task_name=BLACKLIST_PURGE_TASK).exists():
                purge_blacklisted_from_queue(
                    repeat=settings.BLACKLIST_PURGE_INTERVAL_SECONDS, repeat_until=None)

        except Exception:
            # Don't prevent startup if background task scheduling fails
            log.exception("Failed to schedule background tasks")
