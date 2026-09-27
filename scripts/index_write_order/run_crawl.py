"""Crawl, and keep every crawled page before anything ranks it.

The crawl model's negatives have to be what competes for an index page, unfiltered. The
results in the bucket are no good for that: a crawler indexes its batches locally with the
heuristic and submits only what survives. So this runs mwmbl.crawl's batch workers with
submission off, has them push to a key of their own instead of the batch queue, and writes
each batch to devdata/index_write_order/crawl/<date>/ in the format of devdata/batches.

--export-queue also writes out what is already waiting in the local batch queue, without
removing it.

    CRAWL_SUBMIT_MODE=off MWMBL_CONTACT_INFO=https://mwmbl.org CRAWLER_WORKERS=8 PYTHONPATH=. \
        uv run python scripts/index_write_order/run_crawl.py [--export-queue]
"""

import gzip
import json
import time
from argparse import ArgumentParser
from datetime import datetime, timezone
from multiprocessing import Process
from pathlib import Path

import mwmbl.crawl as crawl
from mwmbl.crawler.env_vars import CRAWLER_WORKERS

OUT_DIR = Path("devdata/index_write_order/crawl")
CAPTURE_QUEUE_KEY = "index-write-order-capture"


def write_batch(batch_json: str) -> None:
    batch = json.loads(batch_json)
    crawled = datetime.fromtimestamp(batch["timestamp"] / 1000, timezone.utc)
    day_dir = OUT_DIR / f"{crawled:%Y-%m-%d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{batch['timestamp']}-{abs(hash(batch_json)) % 10**8}.json.gz"
    with gzip.open(path, "wt") as file:
        file.write(batch_json)


def export_queue(crawler: crawl.Crawler) -> None:
    length = crawler.redis.llen(crawl.BATCH_QUEUE_KEY)
    for start in range(0, length, 200):
        for batch_json in crawler.redis.lrange(crawl.BATCH_QUEUE_KEY, start, start + 199):
            write_batch(batch_json)
    print(f"exported {length} queued batches", flush=True)


def main():
    parser = ArgumentParser()
    parser.add_argument("--export-queue", action="store_true")
    args = parser.parse_args()

    crawler = crawl.Crawler()
    crawler.check_redis()
    if args.export_queue:
        export_queue(crawler)
        return

    crawl.validate_environment()
    # The workers are forked, so they push their batches here rather than to the queue a
    # local indexing process would consume.
    crawl.BATCH_QUEUE_KEY = CAPTURE_QUEUE_KEY
    workers = [Process(target=crawler.process_batch_continuously) for _ in range(CRAWLER_WORKERS)]
    for worker in workers:
        worker.start()

    num_batches = 0
    while True:
        batch_jsons = crawler.redis.lpop(CAPTURE_QUEUE_KEY, 10)
        if batch_jsons is None:
            time.sleep(10)
            continue
        for batch_json in batch_jsons:
            write_batch(batch_json)
        num_batches += len(batch_jsons)
        print(f"{datetime.now(timezone.utc):%H:%M:%S} captured {num_batches} batches", flush=True)


if __name__ == "__main__":
    main()
