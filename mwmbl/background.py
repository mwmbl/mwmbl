"""
Script that updates data in a background process.
"""
from mwmbl.indexer import historical
from mwmbl.indexer.preprocess import run_preprocessing
from mwmbl.indexer.retrieve import retrieve_batches
from mwmbl.indexer.update_pages import run_update


def run(index_path: str):
    historical.run()
    while True:
        retrieve_batches()
        run_preprocessing(index_path)
        run_update(index_path)
