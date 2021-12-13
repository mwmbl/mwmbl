import gzip
import json
import os
from glob import glob
from multiprocessing import Process, Lock

from extract_process import fetch_process_warc_records
from fsqueue import FSQueue, GzipJsonRowSerializer
from paths import DATA_DIR

ARCHIVE_INFO_GLOB = 'outputs/records/*.gz'

NUM_PROCESSES = 8


def get_records():
    for path in glob(ARCHIVE_INFO_GLOB):
        with gzip.open(path) as data_file:
            for line in data_file:
                yield json.loads(line)


def process(record):
    print("Record", record)
    return list(fetch_process_warc_records([record]))


def run(lock: Lock):
    input_queue = FSQueue(DATA_DIR, 'records', GzipJsonRowSerializer())
    output_queue = FSQueue(DATA_DIR, 'search-items', GzipJsonRowSerializer())

    while True:
        with lock:
            queue_item = input_queue.get()
        if queue_item is None:
            print("All finished, stopping:", os.getpid())
            break
        item_id, records = queue_item
        print("Got item: ", item_id, os.getpid())
        search_items = []
        for record in records:
            search_items += list(fetch_process_warc_records([record]))
        if search_items:
            output_queue.put(search_items)
        input_queue.done(item_id)


def run_multiprocessing():
    input_queue = FSQueue(DATA_DIR, 'records', GzipJsonRowSerializer())
    input_queue.unlock_all()
    processes = []
    lock = Lock()
    for i in range(NUM_PROCESSES):
        new_process = Process(target=run, args=(lock,))
        new_process.start()
        processes.append(new_process)

    for running_process in processes:
        running_process.join()


if __name__ == '__main__':
    run_multiprocessing()
