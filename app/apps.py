import os
from multiprocessing import Process, Queue

from django.apps import AppConfig

from app import settings
from app.api import queued_batches
from mwmbl import background
from mwmbl.indexer.update_urls import update_urls_continuously
from mwmbl.url_queue import update_queue_continuously


class MwmblConfig(AppConfig):
    name = "app"
    verbose_name = "Mwmbl Application"

    def ready(self):
        if os.environ.get('RUN_MAIN') and settings.RUN_BACKGROUND_PROCESSES:
            new_item_queue = Queue()
            Process(target=background.run, args=(settings.DATA_PATH,)).start()
            Process(target=update_queue_continuously, args=(new_item_queue, queued_batches,)).start()
            Process(target=update_urls_continuously, args=(settings.DATA_PATH, new_item_queue)).start()
