"""
Store for local batches.

We store them in a directory on the local machine.
"""
import gzip
import json
import os
from multiprocessing.pool import ThreadPool
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

from pydantic import ValidationError

from mwmbl.crawler.batch import HashedBatch
from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase, BatchStatus
from mwmbl.retry import retry_requests


class BatchCache:
    num_threads = 20

    def __init__(self, repo_path):
        os.makedirs(repo_path, exist_ok=True)
        self.path = repo_path

    def get_cached(self, batch_urls: list[str]) -> dict[str, HashedBatch]:
        batches = {}
        for url in batch_urls:
            path = self.get_path_from_url(url)
            data = gzip.GzipFile(path).read()
            batch = HashedBatch.parse_raw(data)
            batches[url] = batch
        return batches

    def retrieve_batches(self, num_batches):
        with Database() as db:
            index_db = IndexDatabase(db.connection)
            index_db.create_tables()

        with Database() as db:
            index_db = IndexDatabase(db.connection)
            batches = index_db.get_batches_by_status(BatchStatus.REMOTE, num_batches)
            print(f"Found {len(batches)} remote batches")
            if len(batches) == 0:
                return
            urls = [batch.url for batch in batches]
            pool = ThreadPool(self.num_threads)
            results = pool.imap_unordered(self.retrieve_batch, urls)
            total_processed = 0
            for result in results:
                total_processed += result
            print("Processed batches with items:", total_processed)
            index_db.update_batch_status(urls, BatchStatus.LOCAL)

    def retrieve_batch(self, url):
        data = json.loads(gzip.decompress(retry_requests.get(url).content))
        try:
            batch = HashedBatch.parse_obj(data)
        except ValidationError:
            print("Failed to validate batch", data)
            return 0
        if len(batch.items) > 0:
            self.store(batch, url)
        return len(batch.items)

    def store(self, batch, url):
        path = self.get_path_from_url(url)
        print(f"Storing local batch at {path}")
        os.makedirs(path.parent, exist_ok=True)
        with open(path, 'wb') as output_file:
            data = gzip.compress(batch.json().encode('utf8'))
            output_file.write(data)

    def get_path_from_url(self, url) -> Path:
        url_path = urlparse(url).path
        return Path(self.path) / url_path.lstrip('/')
