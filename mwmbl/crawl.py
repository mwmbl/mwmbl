import logging
import os
import random
import time
from multiprocessing import Process
from pathlib import Path

import django
import requests
from django.conf import settings
from redis import Redis

from mwmbl.crawler.env_vars import CRAWLER_WORKERS, CRAWL_THREADS, CRAWL_DELAY_SECONDS
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import score_result
from mwmbl.tokenizer import tokenize

os.environ["DJANGO_SETTINGS_MODULE"] = "mwmbl.settings_crawler"

data_path = Path(settings.DATA_PATH)
print("Data path", data_path)
data_path.mkdir(exist_ok=True, parents=True)

django.setup()


from mwmbl.indexer.update_urls import record_urls_in_database
from mwmbl.crawler.retrieve import crawl_batch, crawl_url
from mwmbl.crawler.batch import HashedBatch, Result, Results
from mwmbl.indexer.index_batches import index_batches, index_pages

logger = logging.getLogger(__name__)
FORMAT = "%(process)d:%(levelname)s:%(name)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

API_KEY = os.environ["MWMBL_API_KEY"]
BATCH_QUEUE_KEY = "batch-queue"


redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)
url_queue = RedisURLQueue(redis, lambda: set())


def run():
    workers: int = CRAWLER_WORKERS
    assert workers > 0, f"Invalid value for CRAWLER_WORKERS: {workers}"

    batch_processes: list[Process] = []
    for i in range(workers):
        process = Process(target=process_batch_continuously)
        process.start()
        batch_processes.append(process)
        time.sleep(5)

    index_process = Process(target=run_indexing_continuously)
    index_process.start()

    while True:
        if not index_process.is_alive():
            logger.warning("Indexing process [pid={process.pid}] died, respawning.")
            index_process = Process(target=run_indexing_continuously)
            index_process.start()

        for i in range(workers):
            if not batch_processes[i].is_alive():
                logger.info(f"Batch process [pid={batch_processes[i].pid}] died, respawning.")
                batch_processes[i] = Process(target=process_batch_continuously)
                batch_processes[i].start()
                time.sleep(5)

        time.sleep(10)


def process_batch_continuously():
    while True:
        try:
            process_batch()
        except Exception as err:
            logger.exception(f"Error processing batch: '{err}'")
            time.sleep(10)


def process_batch():
    user_id = "test"
    urls = url_queue.get_batch(user_id)
    logger.info(f"Processing batch of {len(urls)} URLs")
    
    # Process URLs sequentially with rate limiting
    results = []
    for i, url in enumerate(urls):
        if i > 0:  # Don't delay before the first URL
            # Add delay with 10% random fuzz
            delay = CRAWL_DELAY_SECONDS * (0.9 + 0.2 * random.random())
            time.sleep(delay)
        
        result = crawl_url(url)
        results.append(result)
        logger.debug("Result", result)
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
        except Exception as err:
            logger.exception(f"Error running indexing: '{err}'")
            time.sleep(10)


def run_indexing():
    index_path = data_path / settings.INDEX_NAME
    batch_jsons = redis.lpop(BATCH_QUEUE_KEY, 10)
    if batch_jsons is None:
        logger.info("No more batches to index. Sleeping for 10 seconds.")
        time.sleep(10)
        return
    logger.info(f"Got {len(batch_jsons)} batches to index")
    batches = [HashedBatch.parse_raw(b) for b in batch_jsons]
    term_new_doc_count = index_batches(batches, index_path)
    logger.info(f"Indexed, top terms to sync: {term_new_doc_count.most_common(10)}")

    remote_index = RemoteIndex()
    with TinyIndex(Document, index_path, 'w') as local_index:
        for term, count in term_new_doc_count.most_common(100):
            logger.info(f"Syncing term {term} with {count} new local items")
            remote_items = remote_index.retrieve(term)
            remote_item_urls = {item.url for item in remote_items}
            local_items = local_index.retrieve(term)
            new_items = [item for item in local_items if item.url not in remote_item_urls]
            logger.info(f"Found {len(new_items)} new items for term {term}")

            terms = tokenize(term)
            remote_item_scores = [score_result(terms, item, True) for item in remote_items]
            min_remote_score = min(remote_item_scores, default=0.0)
            local_scores = [score_result(terms, item, True) for item in new_items]
            max_local_score = max(local_scores, default=0.0)
            logger.info(f"Max local score: {max_local_score}, min remote score: {min_remote_score}")

            new_high_score = max_local_score < min_remote_score

            if new_high_score:
                result_items = [Result(url=doc.url, title=doc.title, extract=doc.extract,
                                       score=doc.score, term=doc.term, state=doc.state) for doc in new_items]
                results = Results(api_key=API_KEY, results=result_items)
                logger.info(f"Posting {len(result_items)} results")
                response = requests.post("https://mwmbl.org/api/v1/crawler/results", json=results.dict())
                logger.info(f"Response: {response.text}")
                response.raise_for_status()

            new_remote_items = remote_index.retrieve(term, refresh=True)
            # Check how many of our items were indexed
            new_remote_item_urls = {item.url for item in new_remote_items}
            indexed_items = sum(1 for item in new_items if item.url in new_remote_item_urls)
            logger.info(f'Indexed items: {indexed_items}/{len(new_items)} for term "{term}"')

            page_index = local_index.get_key_page_index(term)
            index_pages(index_path, {page_index: new_remote_items}, mark_synced=True)
            logger.info(f"Completed indexing for term {term}")

            new_page_content = local_index.get_page(page_index)
            logger.info(f"Page content: {new_page_content}")


if __name__ == "__main__":
    run()
