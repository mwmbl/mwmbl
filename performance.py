"""
Test the performance of the search in terms of compression and speed.
"""
import json
import os
from datetime import datetime
from itertools import islice

from spacy.lang.en import English
from starlette.testclient import TestClient

from app import app, complete
from index import TinyIndexer, index_titles_and_urls, PAGE_SIZE, NUM_PAGES
from paths import TEST_INDEX_PATH
from wiki import get_wiki_titles_and_urls


NUM_DOCUMENTS = 10000


def query_test():
    titles_and_urls = get_wiki_titles_and_urls()

    client = TestClient(app)

    start = datetime.now()
    hits = 0
    for title, url in islice(titles_and_urls, NUM_DOCUMENTS):
        result = client.get('/complete', params={'q': title})
        assert result.status_code == 200
        data = result.content.decode('utf8')
        # data = json.dumps(complete(title))

        if url in data:
            hits += 1

    end = datetime.now()
    print("Hits:", hits)
    print("Query time:", (end - start).total_seconds() / NUM_DOCUMENTS)


def performance_test():
    nlp = English()
    try:
        os.remove(TEST_INDEX_PATH)
    except FileNotFoundError:
        print("No test index found, creating")
    with TinyIndexer(TEST_INDEX_PATH, NUM_PAGES, PAGE_SIZE) as indexer:
        titles_and_urls = get_wiki_titles_and_urls()
        titles_and_urls_slice = islice(titles_and_urls, NUM_DOCUMENTS)

        start_time = datetime.now()
        index_titles_and_urls(indexer, nlp, titles_and_urls_slice)
        stop_time = datetime.now()

        index_time = (stop_time - start_time).total_seconds()
        index_size = os.path.getsize(TEST_INDEX_PATH)

    print("Indexed pages:", NUM_DOCUMENTS)
    print("Index time:", index_time)
    print("Index size", index_size)
    # print("Num tokens", indexer.get_num_tokens())

    # query_test()


if __name__ == '__main__':
    performance_test()
