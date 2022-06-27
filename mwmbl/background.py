"""
Script that updates data in a background process.
"""
from mwmbl.indexer import historical
from mwmbl.indexer.retrieve import retrieve_batches


def run():
    historical.run()
    retrieve_batches()
