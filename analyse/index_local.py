"""
Index batches stored locally on the filesystem for the purpose of evaluation.
"""
import glob
import gzip
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime

import spacy

from mwmbl.crawler.batch import HashedBatch
from mwmbl.crawler.urls import URLDatabase
from mwmbl.database import Database
from mwmbl.indexer.index_batches import index_batches
from mwmbl.tinysearchengine.indexer import TinyIndex, Document

LOCAL_BATCHES_PATH = f'{os.environ["HOME"]}/data/mwmbl/file/**/*.json.gz'
NUM_BATCHES = 10000
EVALUATE_INDEX_PATH = f'{os.environ["HOME"]}/data/mwmbl/evaluate-index.tinysearch'
NUM_PAGES = 1_024_000
PAGE_SIZE = 4096


logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)


def get_batches():
    for path in sorted(glob.glob(LOCAL_BATCHES_PATH, recursive=True))[:NUM_BATCHES]:
        data = json.load(gzip.open(path))
        yield HashedBatch.parse_obj(data)


def run():
    try:
        os.remove(EVALUATE_INDEX_PATH)
    except FileNotFoundError:
        pass
    TinyIndex.create(item_factory=Document, index_path=EVALUATE_INDEX_PATH, num_pages=NUM_PAGES, page_size=PAGE_SIZE)

    batches = get_batches()

    start = datetime.now()
    with Database() as db:
        nlp = spacy.load("en_core_web_sm")
        url_db = URLDatabase(db.connection)
        index_batches(batches, EVALUATE_INDEX_PATH, nlp, url_db)
    end = datetime.now()

    total_time = (end - start).total_seconds()
    print("total_seconds:", total_time)


if __name__ == '__main__':
    run()
