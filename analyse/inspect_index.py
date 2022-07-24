import logging
import sys

import spacy

from mwmbl.indexer.index import tokenize_document
from mwmbl.indexer.paths import INDEX_PATH
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


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
        items = tiny_index.retrieve('search')
        if items:
            for item in items:
                print("Items", item)


def run():
    with TinyIndex(Document, INDEX_PATH) as tiny_index:
        for i in range(100000):
            page = tiny_index.get_page(i)
            for item in page:
                if ' search' in item.title:
                    print("Page", i, item)


if __name__ == '__main__':
    # store()
    # run()
    get_items()
