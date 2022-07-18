"""
Script that updates data in a background process.
"""
from logging import getLogger
from time import sleep

from mwmbl.indexer import historical
from mwmbl.indexer.preprocess import run_preprocessing
from mwmbl.indexer.retrieve import retrieve_batches
from mwmbl.indexer.update_pages import run_update

logger = getLogger(__name__)


def run(index_path: str):
    # historical.run()
    while True:
        # try:
        #     retrieve_batches()
        # except Exception:
        #     logger.exception("Error retrieving batches")
        # try:
        #     run_preprocessing(index_path)
        # except Exception:
        #     logger.exception("Error preprocessing")
        try:
            run_update(index_path)
        except Exception:
            logger.exception("Error running index update")
        sleep(10)
