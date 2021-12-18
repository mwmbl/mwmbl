"""
Index items in the file-system queue
"""
from spacy.lang.en import English

from fsqueue import FSQueue, ZstdJsonSerializer
from index import TinyIndexer, NUM_PAGES, PAGE_SIZE, index_titles_urls_and_extracts
from paths import DATA_DIR, DOMAINS_TITLES_QUEUE_NAME, INDEX_PATH


def get_queue_items():
    titles_queue = FSQueue(DATA_DIR, DOMAINS_TITLES_QUEUE_NAME, ZstdJsonSerializer())
    titles_queue.unlock_all()
    while True:
        items_id, items = titles_queue.get()
        for item in items:
            if item['title'] is None:
                continue
            yield item['title'], item['url']


def index_queue_items():
    nlp = English()
    with TinyIndexer(INDEX_PATH, NUM_PAGES, PAGE_SIZE) as indexer:
        titles_and_urls = get_queue_items()
        index_titles_urls_and_extracts(indexer, nlp, titles_and_urls)


if __name__ == '__main__':
    index_queue_items()
