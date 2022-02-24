"""
Dedupe pages that have been crawled more than once and prepare them for indexing
"""
import glob
import gzip
import json

from mwmbl.indexer.batch import grouper
from mwmbl.indexer.fsqueue import FSQueue, GzipJsonBlobSerializer
from mwmbl.indexer.paths import CRAWL_GLOB, TINYSEARCH_DATA_DIR

BATCH_SIZE = 100


def get_deduped_pages():
    seen_urls = set()
    for path in sorted(glob.glob(CRAWL_GLOB), reverse=True):
        data = json.load(gzip.open(path))
        for item in data['items']:
            url = item['url']
            if url in seen_urls:
                continue

            seen_urls.add(url)
            yield item


def queue_deduped_items(deduped_pages):
    output_queue = FSQueue(TINYSEARCH_DATA_DIR, 'mwmbl-search-items', GzipJsonBlobSerializer())

    for batch in grouper(BATCH_SIZE, deduped_pages):
        data = {'items': batch}
        output_queue.put(data)


def run():
    deduped_pages = get_deduped_pages()
    queue_deduped_items(deduped_pages)


if __name__ == '__main__':
    run()
