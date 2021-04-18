"""
Test the performance of the search in terms of compression and speed.
"""
import os
from datetime import datetime
from itertools import islice

import numpy as np
from spacy.lang.en import English
from starlette.testclient import TestClient

from app import app
from index import TinyIndexer, index_titles_and_urls
from paths import TEST_INDEX_PATH
from wiki import get_wiki_titles_and_urls

NUM_DOCUMENTS = 30000
NUM_PAGES_FOR_STATS = 10
TEST_PAGE_SIZE = 512
TEST_NUM_PAGES = 1024


def query_test():
    titles_and_urls = get_wiki_titles_and_urls()

    client = TestClient(app)

    start = datetime.now()
    hits = 0
    for title, url in islice(titles_and_urls, NUM_DOCUMENTS):
        result = client.get('/complete', params={'q': title})
        assert result.status_code == 200
        data = result.content.decode('utf8')
        # print("Data", data, url, sep='\n')

        if title in data:
            hits += 1

    end = datetime.now()
    print("Hits:", hits)
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
    with TinyIndexer(TEST_INDEX_PATH, TEST_NUM_PAGES, TEST_PAGE_SIZE) as indexer:
        titles_and_urls = get_wiki_titles_and_urls()
        titles_and_urls_slice = islice(titles_and_urls, NUM_DOCUMENTS)

        start_time = datetime.now()
        index_titles_and_urls(indexer, nlp, titles_and_urls_slice)
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

    # query_test()


def print_pages(pages):
    for page in pages:
        for title, url in page:
            print(title, url)
        print()



if __name__ == '__main__':
    performance_test()
