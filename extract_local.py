import gzip
import json
import os
from glob import glob
from pathlib import Path

from extract_process import fetch_process_warc_records
from fsqueue import FSQueue, GzipJsonRowSerializer

DATA_DIR = Path(os.environ['HOME']) / 'data' / 'tinysearch'
EXTRACTS_PATH = DATA_DIR / 'extracts'

ARCHIVE_INFO_GLOB = 'outputs/records/*.gz'


def get_records():
    for path in glob(ARCHIVE_INFO_GLOB):
        with gzip.open(path) as data_file:
            for line in data_file:
                yield json.loads(line)


def process(record):
    print("Record", record)
    return list(fetch_process_warc_records([record]))


def run():
    input_queue = FSQueue(DATA_DIR, 'records', GzipJsonRowSerializer())
    output_queue = FSQueue(DATA_DIR, 'search-items', GzipJsonRowSerializer())

    input_queue.unlock_all()

    while True:
        queue_item = input_queue.get()
        if queue_item is None:
            break
        item_id, records = queue_item
        search_items = []
        for record in records:
            search_items += list(fetch_process_warc_records([record]))
        if search_items:
            output_queue.put(search_items)
        input_queue.done(item_id)


if __name__ == '__main__':
    run()
