"""
Test the performance of the search in terms of compression and speed.
"""
import os
from datetime import datetime

import numpy as np
from spacy.lang.en import English
from starlette.testclient import TestClient

from tinysearchengine import create_app
from fsqueue import ZstdJsonSerializer
from index import index_titles_urls_and_extracts
from tinysearchengine.indexer import TinyIndex, TinyIndexer, Document
from paths import TEST_INDEX_PATH, DATA_DIR, TEST_TERMS_PATH

NUM_DOCUMENTS = 30000
NUM_PAGES_FOR_STATS = 10
TEST_PAGE_SIZE = 512
TEST_NUM_PAGES = 1024
TEST_DATA_PATH = os.path.join(DATA_DIR, 'test-urls.zstd')
RECALL_AT_K = 3

NUM_QUERY_CHARS = 10


def get_test_pages():
    serializer = ZstdJsonSerializer()
    with open(TEST_DATA_PATH, 'rb') as data_file:
        data = serializer.deserialize(data_file.read())
        return [(row['title'], row['url']) for row in data if row['title'] is not None]


def query_test():
    titles_and_urls = get_test_pages()
    print(f"Got {len(titles_and_urls)} titles and URLs")
    tiny_index = TinyIndex(Document, TEST_INDEX_PATH, TEST_NUM_PAGES, TEST_PAGE_SIZE)

    app = create_app.create(tiny_index)
    client = TestClient(app)

    start = datetime.now()
    hits = 0
    count = 0
    for title, url in titles_and_urls:
        query = title[:NUM_QUERY_CHARS]
        result = client.get('/complete', params={'q': query})
        assert result.status_code == 200
        data = result.json()

        hit = False
        if data:
            for result in data[1][:RECALL_AT_K]:
                if url in result:
                    hit = True
                    break

        if hit:
            hits += 1
        else:
            print("Miss", data, title, url, sep='\n')

        count += 1

    end = datetime.now()
    print(f"Hits: {hits} out of {count}")
    print(f"Recall at {RECALL_AT_K}: {hits/count}")
    print("Query time:", (end - start).total_seconds() / NUM_DOCUMENTS)


def page_stats(indexer: TinyIndexer):
    pages_and_sizes = []
    for i in range(TEST_NUM_PAGES):
        page = indexer.get_page(i)
        if page is not None:
            pages_and_sizes.append((len(page), page))
    big_page_sizes, big_pages = zip(*sorted(pages_and_sizes, reverse=True)[:NUM_PAGES_FOR_STATS])
    return np.mean(big_page_sizes), np.std(big_page_sizes), big_pages


def performance_test():
    nlp = English()
    try:
        os.remove(TEST_INDEX_PATH)
    except FileNotFoundError:
        print("No test index found, creating")
    with TinyIndexer(Document, TEST_INDEX_PATH, TEST_NUM_PAGES, TEST_PAGE_SIZE) as indexer:
        titles_and_urls = get_test_pages()

        start_time = datetime.now()
        index_titles_urls_and_extracts(indexer, nlp, titles_and_urls, TEST_TERMS_PATH)
        stop_time = datetime.now()

        index_time = (stop_time - start_time).total_seconds()
        index_size = os.path.getsize(TEST_INDEX_PATH)

        page_size_mean, page_size_std, big_pages = page_stats(indexer)

    print("Indexed pages:", NUM_DOCUMENTS)
    print("Index time:", index_time)
    print("Index size:", index_size)
    print("Mean docs per page:", page_size_mean)
    print("Std err of docs per page:", page_size_std)
    print("Big pages")
    print_pages(big_pages)
    # print("Num tokens", indexer.get_num_tokens())

    query_test()


def print_pages(pages):
    for page in pages:
        print("Page", page)
        for title, url in page:
            print(title, url)
        print()


if __name__ == '__main__':
    performance_test()
