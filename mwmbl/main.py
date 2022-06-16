import argparse
import logging

import pandas as pd
import uvicorn
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from mwmbl.crawler.app import router as crawler_router
from mwmbl.tinysearchengine import search
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

logging.basicConfig()


def setup_args():
    """Read all the args."""
    parser = argparse.ArgumentParser(description="mwmbl-tinysearchengine")
    parser.add_argument("--index", help="Path to the tinysearchengine index file", required=True)
    parser.add_argument("--terms", help="Path to the tinysearchengine terms CSV file", required=True)
    args = parser.parse_args()
    return args


def run():
    """Main entrypoint for tinysearchengine.

    * Parses CLI args
    * Parses and validates config
    * Initializes TinyIndex
    * Initialize a FastAPI app instance
    * Starts uvicorn server using app instance
    """
    args = setup_args()

    # Load term data
    terms = pd.read_csv(args.terms)
    completer = Completer(terms)

    with TinyIndex(item_factory=Document, index_path=args.index) as tiny_index:
        ranker = HeuristicRanker(tiny_index, completer)

        # Initialize FastApi instance
        app = FastAPI()

        # Allow CORS requests from any site
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_headers=["*"],
        )

        search_router = search.create_router(ranker)
        app.include_router(search_router)
        app.include_router(crawler_router)

        # Initialize uvicorn server using global app instance and server config params
        uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    run()
