import logging
import sys

import numpy as np
import spacy

from analyse.index_local import EVALUATE_INDEX_PATH
from mwmbl.indexer import tokenize_document
from mwmbl.indexer import INDEX_PATH
from mwmbl.tinysearchengine import TinyIndex, Document


logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
nlp = spacy.load("en_core_web_sm")


def store():
    document = Document(
        title='A nation in search of the new black | Theatre | The Guardian',
        url='https://www.theguardian.com/stage/2007/nov/18/theatre',
        extract="Topic-stuffed and talk-filled, Kwame Kwei-Armah's new play proves that issue-driven drama is (despite reports of its death) still being written and staged…",
        score=1.0
    )
    with TinyIndex(Document, INDEX_PATH, 'w') as tiny_index:
        tokenized = tokenize_document(document.url, document.title, document.extract, 1, nlp)
        print("Tokenized", tokenized)
        # for token in tokenized.tokens:
        #
        #     tiny_index.index(token, document)


def get_items():
    with TinyIndex(Document, INDEX_PATH) as tiny_index:
        items = tiny_index.retrieve('wikipedia')
        if items:
            for item in items:
                print("Items", item)


def run(index_path):
    with TinyIndex(Document, index_path) as tiny_index:
        sizes = {}
        for i in range(tiny_index.num_pages):
            page = tiny_index.get_page(i)
            if page:
                sizes[i] = len(page)
            if len(page) > 50:
                print("Page", len(page), page)
            # for item in page:
            #     if ' search' in item.title:
            #         print("Page", i, item)
        print("Max", max(sizes.values()))
        print("Top", sorted(sizes.values())[-100:])
        print("Mean", np.mean(list(sizes.values())))


if __name__ == '__main__':
    # store()
    run(EVALUATE_INDEX_PATH)
    # get_items()
