import logging
import os
import time
from collections import Counter
from multiprocessing import Process
from pathlib import Path

import django
import requests
from django.conf import settings
from redis import Redis

from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.tinysearchengine.indexer import TinyIndex, Document

os.environ["DJANGO_SETTINGS_MODULE"] = "mwmbl.settings_crawler"

data_path = Path(settings.DATA_PATH)
print("Data path", data_path)
data_path.mkdir(exist_ok=True, parents=True)

django.setup()


from mwmbl.indexer.update_urls import record_urls_in_database
from mwmbl.crawler.retrieve import crawl_batch
from mwmbl.crawler.batch import HashedBatch, Result, Results
from mwmbl.indexer.index_batches import index_batches


logger = logging.getLogger(__name__)
FORMAT = "%(process)d:%(levelname)s:%(name)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

API_KEY = os.environ["MWMBL_API_KEY"]
BATCH_QUEUE_KEY = "batch-queue"


redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)
url_queue = RedisURLQueue(redis, lambda: set())


def run():
    # for i in range(10):
    #     process = Process(target=process_batch_continuously)
    #     process.start()
    #     time.sleep(5)

    index_process = Process(target=run_indexing_continuously)
    index_process.start()


def process_batch_continuously():
    while True:
        try:
            process_batch()
        except Exception:
            logger.exception("Error processing batch")
            time.sleep(10)


def process_batch():
    user_id = "test"
    urls = url_queue.get_batch(user_id)
    results = crawl_batch(urls, 20)
    for result in results:
        print("Result", result)
    js_timestamp = int(time.time() * 1000)
    batch = HashedBatch.parse_obj({"user_id_hash": user_id, "timestamp": js_timestamp, "items": results})
    record_urls_in_database([batch], url_queue)

    # Push the batch into the Redis queue
    batch_json = batch.json()
    redis.rpush(BATCH_QUEUE_KEY, batch_json)


def run_indexing_continuously():
    while True:
        try:
            run_indexing()
        except Exception:
            logger.exception("Error running indexing")
            time.sleep(10)


def run_indexing():
    index_path = data_path / settings.INDEX_NAME
    batch_jsons = redis.lpop(BATCH_QUEUE_KEY, 1)
    if batch_jsons is None:
        logger.info("No more batches to index. Sleeping for 10 seconds.")
        time.sleep(10)
        return
    logger.info(f"Got {len(batch_jsons)} batches to index")
    batches = [HashedBatch.parse_raw(b) for b in batch_jsons]
    term_new_docs = index_batches(batches, index_path)
    term_new_doc_count = Counter({term: len(docs) for term, docs in term_new_docs.items()})
    logger.info(f"Indexed, top terms to sync: {term_new_doc_count.most_common(10)}")

    remote_index = RemoteIndex()
    with TinyIndex(Document, index_path, 'r') as local_index:
        for term, count in term_new_doc_count.most_common(10):
            remote_items = remote_index.retrieve(term)
            remote_item_urls = {item.url for item in remote_items}
            local_items = local_index.retrieve(term)
            new_items = [item for item in local_items if item.url not in remote_item_urls]
            logger.info(f"Found {len(new_items)} new items for term {term}")
            for item in new_items:
                logger.info(f"New item: {item}")

            result_items = [Result(url=doc.url, title=doc.title, extract=doc.extract,
                                   score=doc.score, term=doc.term, state=doc.state) for doc in new_items]
            results = Results(api_key=API_KEY, results=result_items)
            response = requests.post("https://beta.mwmbl.org/api/v1/crawler/results", json=results.dict())
            print("Response", response.text)
            response.raise_for_status()





if __name__ == "__main__":
    run()
