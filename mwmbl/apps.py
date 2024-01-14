import faulthandler
import sys
from multiprocessing import Process, Queue
from pathlib import Path

from django.apps import AppConfig
from django.conf import settings

from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase


class MwmblConfig(AppConfig):
    name = "mwmbl"
    verbose_name = "Mwmbl Application"

    def ready(self):
        # Imports here to avoid AppRegistryNotReady exception
        from mwmbl.search_setup import queued_batches
        from mwmbl import background
        from mwmbl.indexer.paths import INDEX_NAME
        from mwmbl.indexer.update_urls import update_urls_continuously
        from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE
        from mwmbl.url_queue import update_queue_continuously

        faulthandler.enable(file=sys.stdout)

        index_path = Path(settings.DATA_PATH) / INDEX_NAME
        try:
            existing_index = TinyIndex(item_factory=Document, index_path=index_path)
            if existing_index.page_size != PAGE_SIZE or existing_index.num_pages != settings.NUM_PAGES:
                raise ValueError(f"Existing index page sizes ({existing_index.page_size}) or number of pages "
                                 f"({existing_index.num_pages}) do not match")
        except FileNotFoundError:
            print("Creating a new index")
            TinyIndex.create(item_factory=Document, index_path=index_path, num_pages=settings.NUM_PAGES,
                             page_size=PAGE_SIZE)

        with Database() as db:
            index_db = IndexDatabase(db.connection)
            index_db.create_tables()

        if settings.RUN_BACKGROUND_PROCESSES:
            new_item_queue = Queue()
            Process(target=background.run, args=(settings.DATA_PATH,)).start()
            Process(target=update_queue_continuously, args=(new_item_queue, queued_batches,)).start()
            Process(target=update_urls_continuously, args=(settings.DATA_PATH, new_item_queue)).start()
