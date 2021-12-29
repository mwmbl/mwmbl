import logging
import sys
from typing import Optional
import argparse
from fastapi import FastAPI
import uvicorn

from mwmbl.tinysearchengine import create_app
from mwmbl.tinysearchengine.indexer import TinyIndex, NUM_PAGES, PAGE_SIZE, Document
from mwmbl.tinysearchengine.config import parse_config_file

logging.basicConfig()

app: Optional[FastAPI] = None


def setup_args():
    """Read all the args."""
    parser = argparse.ArgumentParser(description="mwmbl-tinysearchengine")
    parser.add_argument("--config", help="Path to tinysearchengine's yaml config.", required=True)
    args = parser.parse_args()
    return args


def main():
    """Main entrypoint for tinysearchengine.

    * Parses CLI args
    * Parses and validates config
    * Initializes TinyIndex
    * Populates global app (FastAPI) variable so that uvicorn can run the app server
    """
    args = setup_args()
    config = parse_config_file(config_filename=args.config)

    # Initialize TinyIndex using index config params
    tiny_index = TinyIndex(
        item_factory=Document,
        **config.index_config.dict()
    )

    # Update global app variable
    global app
    app = create_app.create(tiny_index)

    # Initialize uvicorn server using global app instance and server config params
    uvicorn.run(
        "mwmbl.tinysearchengine.app:app",
        **config.server_config.dict()
    )


if __name__ == "__main__":
    main()
