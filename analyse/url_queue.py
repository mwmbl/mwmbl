import logging
import os
import pickle
import sys
from datetime import datetime
from pathlib import Path
from queue import Queue

from mwmbl.url_queue import URLQueue

FORMAT = '%(levelname)s %(name)s %(asctime)s %(message)s'
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG, format=FORMAT)


def run_url_queue():
    data = pickle.load(open(Path(os.environ["HOME"]) / "data" / "mwmbl" / "found-urls.pickle", "rb"))
    print("First URLs", [x.url for x in data[:1000]])

    new_item_queue = Queue()
    queued_batches = Queue()
    queue = URLQueue(new_item_queue, queued_batches)

    new_item_queue.put(data)

    start = datetime.now()
    queue.update()
    total_time = (datetime.now() - start).total_seconds()
    print(f"Total time: {total_time}")





if __name__ == '__main__':
    run_url_queue()
