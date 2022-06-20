"""
Retrieve remote batches and store them in Postgres locally
"""
import gzip
import json
from multiprocessing.pool import ThreadPool
from time import sleep

import requests

from mwmbl.crawler.app import HashedBatch
from mwmbl.database import Database
from mwmbl.indexdb import IndexDatabase, BatchStatus
from mwmbl.tinysearchengine.indexer import Document

NUM_THREADS = 10


def retrieve_batches():
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.create_tables()

    with Database() as db:
        index_db = IndexDatabase(db.connection)
        batches = index_db.get_batches_by_status(BatchStatus.REMOTE)
        print("Batches", batches)
        urls = [batch.url for batch in batches][:10]
        pool = ThreadPool(NUM_THREADS)
        results = pool.imap_unordered(retrieve_batch, urls)
        for result in results:
            print("Processed batch with items:", result)

        with Database() as db:
            index_db = IndexDatabase(db.connection)
            index_db.update_batch_status(urls, BatchStatus.LOCAL)


def retrieve_batch(url):
    data = json.loads(gzip.decompress(requests.get(url).content))
    batch = HashedBatch.parse_obj(data)
    queue_batch(url, batch)
    return len(batch.items)


def queue_batch(batch_url: str, batch: HashedBatch):
    # TODO get the score from the URLs database
    documents = [Document(item.content.title, item.url, item.content.extract, 1)
                 for item in batch.items if item.content is not None]
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.queue_documents(documents)


def run():
    while True:
        retrieve_batches()
        sleep(10)


if __name__ == '__main__':
    retrieve_batches()
