"""
Add domains to the queue to be retrieved
"""
import csv
import gzip

from persistqueue import SQLiteQueue, SQLiteAckQueue

from paths import DOMAINS_QUEUE_PATH, DOMAINS_PATH


def get_domains():
    reader = csv.reader(gzip.open(DOMAINS_PATH, 'rt'))
    next(reader)
    for rank, domain, _ in reader:
        yield rank, domain


def queue_domains():
    queue = SQLiteAckQueue(DOMAINS_QUEUE_PATH)
    queued = 0
    for rank, domain in get_domains():
        queue.put((rank, domain))
        queued += 1
        if queued % 1000 == 0:
            print("Queued:", queued)


if __name__ == '__main__':
    queue_domains()
