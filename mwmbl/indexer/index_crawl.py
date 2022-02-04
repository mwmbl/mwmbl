"""
Index data crawled through the Mwmbl crawler.
"""
from logging import getLogger

import spacy

from mwmbl.indexer.fsqueue import FSQueue, GzipJsonBlobSerializer, FSQueueError
from mwmbl.indexer.index import index_titles_urls_and_extracts
from mwmbl.indexer.paths import INDEX_PATH, MWMBL_CRAWL_TERMS_PATH, DATA_DIR
from mwmbl.tinysearchengine.indexer import TinyIndexer, Document, NUM_PAGES, PAGE_SIZE


logger = getLogger(__name__)


def index_mwmbl_craw_data():
    nlp = spacy.load("en_core_web_sm")

    with TinyIndexer(Document, INDEX_PATH, NUM_PAGES, PAGE_SIZE) as indexer:
        titles_urls_and_extracts = get_mwmbl_crawl_titles_urls_and_extracts()
        index_titles_urls_and_extracts(indexer, nlp, titles_urls_and_extracts, MWMBL_CRAWL_TERMS_PATH)


def get_mwmbl_crawl_titles_urls_and_extracts():
    input_queue = FSQueue(DATA_DIR, 'mwmbl-search-items', GzipJsonBlobSerializer())
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
        item_id, item_data = next_item
        logger.info(f'Processing item {item_id}')
        for item in item_data['items']:
            yield item['title'], item['url'], item['extract']
        input_queue.done(item_id)


if __name__ == '__main__':
    index_mwmbl_craw_data()
