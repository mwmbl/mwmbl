"""
Retrieve remote batches and store them in Postgres locally
"""
import gzip
import json
import traceback
from multiprocessing.pool import ThreadPool
from time import sleep

from pydantic import ValidationError

from mwmbl.crawler.app import create_historical_batch, queue_batch
from mwmbl.crawler.batch import HashedBatch
from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase, BatchStatus
from mwmbl.retry import retry_requests

NUM_THREADS = 5


def retrieve_batches():
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.create_tables()

    with Database() as db:
        index_db = IndexDatabase(db.connection)

        for i in range(100):
            batches = index_db.get_batches_by_status(BatchStatus.REMOTE)
            print(f"Found {len(batches)} remote batches")
            if len(batches) == 0:
                return
            urls = [batch.url for batch in batches]
            pool = ThreadPool(NUM_THREADS)
            results = pool.imap_unordered(retrieve_batch, urls)
            for result in results:
                if result > 0:
                    print("Processed batch with items:", result)
            index_db.update_batch_status(urls, BatchStatus.LOCAL)


def retrieve_batch(url):
    data = json.loads(gzip.decompress(retry_requests.get(url).content))
    try:
        batch = HashedBatch.parse_obj(data)
    except ValidationError:
        print("Failed to validate batch", data)
        return 0
    if len(batch.items) > 0:
        print(f"Retrieved batch with {len(batch.items)} items")
        create_historical_batch(batch)
        queue_batch(batch)
    return len(batch.items)


def run():
    while True:
        try:
            retrieve_batches()
        except Exception as e:
            print("Exception retrieving batch")
            traceback.print_exception(type(e), e, e.__traceback__)
        sleep(10)


if __name__ == '__main__':
    retrieve_batches()
