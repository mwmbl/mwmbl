"""
Test the performance of the search in terms of compression and speed.
"""
import os
from datetime import datetime
from itertools import islice

from spacy.lang.en import English

from index import Indexer, index_titles_and_urls
from paths import TEST_INDEX_PATH
from wiki import get_wiki_titles_and_urls


def performance_test():
    nlp = English()
    try:
        os.remove(TEST_INDEX_PATH)
    except FileNotFoundError:
        print("No test index found, creating")
    indexer = Indexer(TEST_INDEX_PATH)
    titles_and_urls = get_wiki_titles_and_urls()
    titles_and_urls_slice = islice(titles_and_urls, 50000)

    start_time = datetime.now()
    index_titles_and_urls(indexer, nlp, titles_and_urls_slice)
    stop_time = datetime.now()

    index_time = (stop_time - start_time).total_seconds()
    index_size = os.path.getsize(TEST_INDEX_PATH)

    print("Index time:", index_time)
    print("Index size", index_size)
    print("Num tokens", indexer.get_num_tokens())


if __name__ == '__main__':
    performance_test()
