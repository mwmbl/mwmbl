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
        if settings.SETUP_DATABASE:
            create_index_db()
        self._schedule_background_tasks()

    @staticmethod
    def _schedule_background_tasks():
        """
        Schedule periodic background tasks if they are not already queued.
        Uses django-background-tasks; requires `manage.py process_tasks` to be running.
        """
        try:
            from background_task.models import Task
            from mwmbl.background import sync_search_counts

            SYNC_TASK = "mwmbl.background.sync_search_counts"

            # Sync search counts once per hour (3600 seconds)
            if not Task.objects.filter(task_name=SYNC_TASK).exists():
                sync_search_counts(repeat=3600, repeat_until=None)

        except Exception:
            # Don't prevent startup if background task scheduling fails
            import logging
            logging.getLogger(__name__).exception(
                "Failed to schedule background tasks"
            )
