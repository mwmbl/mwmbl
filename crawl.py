import logging
import os
import time
from pathlib import Path

import django
from django.conf import settings

os.environ["DJANGO_SETTINGS_MODULE"] = "mwmbl.settings_crawler"

data_path = Path(settings.DATA_PATH)
print("Data path", data_path)
data_path.mkdir(exist_ok=True, parents=True)

django.setup()


from mwmbl.indexer.update_urls import record_urls_in_database
from mwmbl.crawler.retrieve import crawl_batch
from mwmbl.search_setup import queued_batches as url_queue
from mwmbl.crawler.batch import HashedBatch
from mwmbl.indexer.index_batches import index_batches


logging.basicConfig(level=logging.INFO)


def run():
    user_id = "test"
    urls = url_queue.get_batch(user_id)
    results = crawl_batch(list(urls)[:10], 20)
    for result in results:
        print("Result", result)

    js_timestamp = int(time.time() * 1000)
    batch = HashedBatch.parse_obj({"user_id_hash": user_id, "timestamp": js_timestamp, "items": results})
    record_urls_in_database([batch], url_queue)
    index_path = data_path / settings.INDEX_NAME

    index_batches([batch], index_path)


if __name__ == "__main__":
    run()
