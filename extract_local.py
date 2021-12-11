import gzip
import json
import multiprocessing
import os
from glob import glob
from itertools import islice
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
    queue = FSQueue(DATA_DIR, 'records', GzipJsonRowSerializer())
    path, records = queue.get()
    for record in records:
        result = process(record)
        print("Result", result)


    # with gzip.open(EXTRACTS_PATH / 'data.json.gz', 'wt') as output_file:
    #     for row in processed:
    #         output_file.write(json.dumps(row) + '\n')
    #         print("Processed", row)


if __name__ == '__main__':
    run()
