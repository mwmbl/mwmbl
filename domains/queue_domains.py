"""
Add domains to the queue to be retrieved
"""
import csv
import gzip

from fsqueue import FSQueue, ZstdJsonSerializer
from paths import DOMAINS_PATH, DOMAINS_QUEUE_NAME, DATA_DIR

BATCH_SIZE = 10000


def get_domains():
    reader = csv.reader(gzip.open(DOMAINS_PATH, 'rt'))
    next(reader)
    for rank, domain, _ in reader:
        yield rank, domain


def queue_domains():
    queue = FSQueue(DATA_DIR, DOMAINS_QUEUE_NAME, ZstdJsonSerializer())
    queued = 0
    batch = []
    for rank, domain in get_domains():
        batch.append((rank, domain))
        queued += 1
        if queued % BATCH_SIZE == 0:
            queue.put(batch)
            batch = []
            print("Queued:", queued)


if __name__ == '__main__':
    queue_domains()
