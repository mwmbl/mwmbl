import argparse
import logging
import os
import sys
from multiprocessing import Process, Queue
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from mwmbl import background
from mwmbl.crawler import app as crawler
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.paths import INDEX_NAME, BATCH_DIR_NAME
from mwmbl.tinysearchengine import search
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE
from mwmbl.tinysearchengine.rank import HeuristicRanker

logging.basicConfig(stream=sys.stdout, level=logging.INFO)


MODEL_PATH = Path(__file__).parent / 'resources' / 'model.pickle'


def setup_args():
    parser = argparse.ArgumentParser(description="Mwmbl API server and background task processor")
    parser.add_argument("--num-pages", type=int, help="Number of pages of memory (4096 bytes) to use for the index", default=2560)
    parser.add_argument("--data", help="Path to the data folder for storing index and cached batches", default="./devdata")
    parser.add_argument("--background", help="Enable running the background tasks to process batches",
                        action='store_true')
    args = parser.parse_args()
    return args


def run():
    args = setup_args()

    index_path = Path(args.data) / INDEX_NAME
    try:
        existing_index = TinyIndex(item_factory=Document, index_path=index_path)
        if existing_index.page_size != PAGE_SIZE or existing_index.num_pages != args.num_pages:
            raise ValueError(f"Existing index page sizes ({existing_index.page_size}) or number of pages "
                             f"({existing_index.num_pages}) do not match")
    except FileNotFoundError:
        print("Creating a new index")
        TinyIndex.create(item_factory=Document, index_path=index_path, num_pages=args.num_pages, page_size=PAGE_SIZE)

    url_queue = Queue()

    if args.background:
        Process(target=background.run, args=(args.data, url_queue)).start()

    completer = Completer()

    with TinyIndex(item_factory=Document, index_path=index_path) as tiny_index:
        ranker = HeuristicRanker(tiny_index, completer)
        # model = pickle.load(open(MODEL_PATH, 'rb'))
        # ranker = LTRRanker(model, tiny_index, completer)

        # Initialize FastApi instance
        app = FastAPI()

        search_router = search.create_router(ranker)
        app.include_router(search_router)

        batch_cache = BatchCache(Path(args.data) / BATCH_DIR_NAME)
        crawler_router = crawler.get_router(batch_cache, url_queue)
        app.include_router(crawler_router)

        # Initialize uvicorn server using global app instance and server config params
        uvicorn.run(app, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    run()
