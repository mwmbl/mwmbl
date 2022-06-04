"""
Add domains to the queue to be retrieved
"""
import csv
import gzip

from mwmbl.indexer.fsqueue import FSQueue, ZstdJsonSerializer
from mwmbl.indexer.paths import DOMAINS_PATH, DOMAINS_QUEUE_NAME, TINYSEARCH_DATA_DIR

BATCH_SIZE = 250


def get_domains():
    reader = csv.reader(gzip.open(DOMAINS_PATH, 'rt'))
    next(reader)
    for rank, domain, _ in reader:
        yield rank, domain


def queue_domains():
    queue = FSQueue(TINYSEARCH_DATA_DIR, DOMAINS_QUEUE_NAME, ZstdJsonSerializer())
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
