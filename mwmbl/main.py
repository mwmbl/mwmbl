import argparse
import logging
import os
from multiprocessing import Process

import uvicorn
from fastapi import FastAPI

from mwmbl import background
from mwmbl.indexer import historical, retrieve, preprocess, update_pages
from mwmbl.crawler.app import router as crawler_router
from mwmbl.tinysearchengine import search
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, NUM_PAGES, PAGE_SIZE
from mwmbl.tinysearchengine.rank import HeuristicRanker

logging.basicConfig()


def setup_args():
    parser = argparse.ArgumentParser(description="mwmbl-tinysearchengine")
    parser.add_argument("--index", help="Path to the tinysearchengine index file", default="/app/storage/index.tinysearch")
    args = parser.parse_args()
    return args


def run():
    args = setup_args()

    try:
        existing_index = TinyIndex(item_factory=Document, index_path=args.index)
        if existing_index.page_size != PAGE_SIZE or existing_index.num_pages != NUM_PAGES:
            print(f"Existing index page sizes ({existing_index.page_size}) and number of pages "
                  f"({existing_index.num_pages}) does not match - removing.")
            os.remove(args.index)
            existing_index = None
    except FileNotFoundError:
        existing_index = None

    if existing_index is None:
        print("Creating a new index")
        TinyIndex.create(item_factory=Document, index_path=args.index, num_pages=NUM_PAGES, page_size=PAGE_SIZE)

    Process(target=background.run, args=(args.index,)).start()
    # Process(target=historical.run).start()
    # Process(target=retrieve.run).start()
    # Process(target=preprocess.run, args=(args.index,)).start()
    # Process(target=update_pages.run, args=(args.index,)).start()

    completer = Completer()

    with TinyIndex(item_factory=Document, index_path=args.index) as tiny_index:
        ranker = HeuristicRanker(tiny_index, completer)

        # Initialize FastApi instance
        app = FastAPI()

        search_router = search.create_router(ranker)
        app.include_router(search_router)
        app.include_router(crawler_router)

        # Initialize uvicorn server using global app instance and server config params
        uvicorn.run(app, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    run()
