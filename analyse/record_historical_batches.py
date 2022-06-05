"""
See how many unique URLs and root domains we have crawled.
"""
import glob
import gzip
import json
from collections import defaultdict, Counter
from urllib.parse import urlparse

import requests

from mwmbl.indexer.paths import CRAWL_GLOB


API_ENDPOINT = "http://localhost:8080/batches/historical"


def get_batches():
    for path in glob.glob(CRAWL_GLOB):
        hashed_batch = json.load(gzip.open(path))
        yield hashed_batch


def run():
    batches = get_batches()
    for hashed_batch in batches:
        print("Recording batch", hashed_batch)
        response = requests.post(API_ENDPOINT, json=hashed_batch)
        print("Response", response)


if __name__ == '__main__':
    run()

