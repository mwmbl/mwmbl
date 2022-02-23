import argparse
import logging

import pandas as pd
import uvicorn

from mwmbl.tinysearchengine import create_app
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import Ranker

logging.basicConfig()


def setup_args():
    """Read all the args."""
    parser = argparse.ArgumentParser(description="mwmbl-tinysearchengine")
    parser.add_argument("--index", help="Path to the tinysearchengine index file", required=True)
    parser.add_argument("--terms", help="Path to the tinysearchengine terms CSV file", required=True)
    args = parser.parse_args()
    return args


def main():
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
        ranker = Ranker(tiny_index, completer)

        # Initialize FastApi instance
        app = create_app.create(ranker)

        # Initialize uvicorn server using global app instance and server config params
        uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
