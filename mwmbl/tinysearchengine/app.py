import logging
import argparse
import uvicorn

from mwmbl.tinysearchengine import create_app
from mwmbl.tinysearchengine.indexer import TinyIndex, NUM_PAGES, PAGE_SIZE, Document
from mwmbl.tinysearchengine.config import parse_config_file

logging.basicConfig()


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
    * Initialize a FastAPI app instance
    * Starts uvicorn server using app instance
    """
    args = setup_args()
    config = parse_config_file(config_filename=args.config)

    # Initialize TinyIndex using index config params
    tiny_index = TinyIndex(
        item_factory=Document,
        **config.index_config.dict()
    )

    # Initialize FastApi instance
    app = create_app.create(tiny_index)

    # Initialize uvicorn server using global app instance and server config params
    uvicorn.run(app, **config.server_config.dict())


if __name__ == "__main__":
    main()
