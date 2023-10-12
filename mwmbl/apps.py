import os
import shutil
from multiprocessing import Process, Queue
from pathlib import Path

from django.apps import AppConfig
from django.conf import settings

from mwmbl.api import queued_batches
from mwmbl import background
from mwmbl.indexer.paths import INDEX_NAME
from mwmbl.indexer.update_urls import update_urls_continuously
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE
from mwmbl.url_queue import update_queue_continuously


class MwmblConfig(AppConfig):
    name = "mwmbl"
    verbose_name = "Mwmbl Application"

    def ready(self):
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

        if settings.RUN_BACKGROUND_PROCESSES:
            new_item_queue = Queue()
            Process(target=background.run, args=(settings.DATA_PATH,)).start()
            Process(target=update_queue_continuously, args=(new_item_queue, queued_batches,)).start()
            Process(target=update_urls_continuously, args=(settings.DATA_PATH, new_item_queue)).start()

        if not settings.DEBUG:
            # Remove all existing content from the static folder:
            # https://stackoverflow.com/a/1073382
            for root, dirs, files in os.walk('/app/static'):
                for f in files:
                    os.unlink(os.path.join(root, f))
                for d in dirs:
                    shutil.rmtree(os.path.join(root, d))

            shutil.copytree('/front-end-build', '/app/static')
