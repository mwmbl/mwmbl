"""
Store for local batches.

We store them in a directory on the local machine.
"""
import gzip
import json
import os
from multiprocessing.pool import ThreadPool
from tempfile import NamedTemporaryFile

from pydantic import ValidationError

from mwmbl.crawler.batch import HashedBatch
from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase, BatchStatus
from mwmbl.retry import retry_requests


class BatchCache:
    num_threads = 8

    def __init__(self, repo_path):
        os.makedirs(repo_path, exist_ok=True)
        self.path = repo_path

    def store(self, batch: HashedBatch):
        with NamedTemporaryFile(mode='w', dir=self.path, prefix='batch_', suffix='.json', delete=False) as output_file:
            output_file.write(batch.json())

    def get(self, num_batches) -> dict[str, HashedBatch]:
        batches = {}
        for path in os.listdir(self.path):
            batch = HashedBatch.parse_file(path)
            while len(batches) < num_batches:
                batches[path] = batch
        return batches

    def retrieve_batches(self, num_thousand_batches=10):
        with Database() as db:
            index_db = IndexDatabase(db.connection)
            index_db.create_tables()

        with Database() as db:
            index_db = IndexDatabase(db.connection)

            for i in range(num_thousand_batches):
                batches = index_db.get_batches_by_status(BatchStatus.REMOTE)
                print(f"Found {len(batches)} remote batches")
                if len(batches) == 0:
                    return
                urls = [batch.url for batch in batches]
                pool = ThreadPool(self.num_threads)
                results = pool.imap_unordered(self.retrieve_batch, urls)
                for result in results:
                    if result > 0:
                        print("Processed batch with items:", result)
                index_db.update_batch_status(urls, BatchStatus.LOCAL)

    def retrieve_batch(self, url):
        data = json.loads(gzip.decompress(retry_requests.get(url).content))
        try:
            batch = HashedBatch.parse_obj(data)
        except ValidationError:
            print("Failed to validate batch", data)
            return 0
        if len(batch.items) > 0:
            self.store(batch)
        return len(batch.items)
