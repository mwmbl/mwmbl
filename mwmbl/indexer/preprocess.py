"""
Preprocess local documents for indexing.
"""
import traceback
from time import sleep

import spacy

from mwmbl.database import Database
from mwmbl.indexer.indexdb import IndexDatabase
from mwmbl.indexer.index import tokenize_document
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


def run(index_path):
    while True:
        try:
            run_preprocessing(index_path)
        except Exception as e:
            print("Exception preprocessing")
            traceback.print_exception(type(e), e, e.__traceback__)
            sleep(10)


def run_preprocessing(index_path):
    nlp = spacy.load("en_core_web_sm")
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        documents = index_db.get_documents_for_preprocessing()
        print(f"Got {len(documents)} documents for preprocessing")
        if len(documents) == 0:
            sleep(10)
        with TinyIndex(Document, index_path, 'w') as indexer:
            for document in documents:
                tokenized = tokenize_document(document.url, document.title, document.extract, 1, nlp)
                page_indexes = [indexer.get_key_page_index(token) for token in tokenized.tokens]
                index_db.queue_documents_for_page([(tokenized.url, i) for i in page_indexes])


if __name__ == '__main__':
    run('data/index.tinysearch')