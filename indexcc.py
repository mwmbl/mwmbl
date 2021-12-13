"""
Index data downloaded from Common Crawl
"""

import spacy

from fsqueue import FSQueue, GzipJsonRowSerializer
from index import TinyIndexer, index_titles_and_urls, PAGE_SIZE, NUM_PAGES, Document
from paths import INDEX_PATH, DATA_DIR, COMMON_CRAWL_TERMS_PATH


def index_common_craw_data():
    nlp = spacy.load("en_core_web_sm")

    with TinyIndexer(Document, INDEX_PATH, NUM_PAGES, PAGE_SIZE) as indexer:
        titles_and_urls = get_common_crawl_titles_and_urls()
        index_titles_and_urls(indexer, nlp, titles_and_urls, COMMON_CRAWL_TERMS_PATH)


def get_common_crawl_titles_and_urls():
    input_queue = FSQueue(DATA_DIR, 'search-items', GzipJsonRowSerializer())
    while True:
        next_item = input_queue.get()
        if next_item is None:
            break
        item_id, items = next_item
        for url, title, extract in items:
            yield title, url


if __name__ == '__main__':
    index_common_craw_data()
