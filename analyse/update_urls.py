import os
import pickle
from datetime import datetime
from pathlib import Path
from queue import Queue

from mwmbl.indexer import record_urls_in_database


def run_update_urls_on_fixed_batches():
    with open(Path(os.environ["HOME"]) / "data" / "mwmbl" / "hashed-batches.pickle", "rb") as file:
        batches = pickle.load(file)

    # print("Batches", batches[:3])

    queue = Queue()

    start = datetime.now()
    record_urls_in_database(batches, queue)
    total_time = (datetime.now() - start).total_seconds()

    print("Total time:", total_time)


if __name__ == '__main__':
    run_update_urls_on_fixed_batches()
