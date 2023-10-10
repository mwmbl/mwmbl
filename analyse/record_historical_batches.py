"""
See how many unique URLs and root domains we have crawled.
"""
import glob
import gzip
import json

import requests

from mwmbl.indexer import CRAWL_GLOB


API_ENDPOINT = "http://95.216.215.29/batches/historical"


def total_num_batches():
    return len(glob.glob(CRAWL_GLOB))


def get_batches():
    for path in sorted(glob.glob(CRAWL_GLOB)):
        hashed_batch = json.load(gzip.open(path))
        yield hashed_batch


def convert_item(item):
    return {
        'url': item['url'],
        'status': 200,
        'timestamp': item['timestamp'],
        'content': {
            'title': item['title'],
            'extract': item['extract'],
            'links': item['links'],
        }
    }



def run():
    total_batches = total_num_batches()
    batches = get_batches()
    for i, hashed_batch in enumerate(batches):
        new_batch = {
            'user_id_hash': hashed_batch['user_id_hash'],
            'timestamp': hashed_batch['timestamp'],
            'items': [convert_item(item) for item in hashed_batch['items']]
        }
        response = requests.post(API_ENDPOINT, json=new_batch)
        print(f"Response {i} of {total_batches}", response)


if __name__ == '__main__':
    run()

