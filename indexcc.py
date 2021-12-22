"""
Index data downloaded from Common Crawl
"""
import logging
import sys
from logging import getLogger

import spacy

from fsqueue import FSQueue, GzipJsonRowSerializer, FSQueueError
from index import index_titles_urls_and_extracts
from tinysearchengine.indexer import TinyIndexer, NUM_PAGES, PAGE_SIZE, Document
from paths import INDEX_PATH, DATA_DIR, COMMON_CRAWL_TERMS_PATH


logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
logger = getLogger(__name__)


def index_common_craw_data():
    nlp = spacy.load("en_core_web_sm")

    with TinyIndexer(Document, INDEX_PATH, NUM_PAGES, PAGE_SIZE) as indexer:
        titles_urls_and_extracts = get_common_crawl_titles_urls_and_extracts()
        index_titles_urls_and_extracts(indexer, nlp, titles_urls_and_extracts, COMMON_CRAWL_TERMS_PATH)


def get_common_crawl_titles_urls_and_extracts():
    input_queue = FSQueue(DATA_DIR, 'search-items', GzipJsonRowSerializer())
    input_queue.unlock_all()
    while True:
        try:
            next_item = input_queue.get()
        except FSQueueError as e:
            logger.exception(f'Error with item {e.item_id}')
            input_queue.error(e.item_id)
            continue
        if next_item is None:
            logger.info('Not more items to process, stopping')
            break
        item_id, items = next_item
        logger.info(f'Processing item {item_id}')
        for url, title, extract in items:
            yield title, url, extract
        input_queue.done(item_id)


if __name__ == '__main__':
    index_common_craw_data()
