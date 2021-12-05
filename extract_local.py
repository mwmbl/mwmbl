import gzip
import json
from glob import glob
from itertools import islice

from extract_process import fetch_process_warc_records

ARCHIVE_INFO_GLOB = 'outputs/records/*.gz'


def get_records():
    for path in glob(ARCHIVE_INFO_GLOB):
        with gzip.open(path) as data_file:
            for line in data_file:
                yield json.loads(line)


def run():
    records = get_records()
    processed = fetch_process_warc_records(islice(records, 10))
    for row in processed:
        print("Processed", row)


if __name__ == '__main__':
    run()
