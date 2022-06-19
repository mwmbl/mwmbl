from datetime import date, datetime

import spacy

from mwmbl.crawler.app import get_user_id_hashes_for_date, get_batches_for_date_and_user, get_batch_from_id, \
    create_historical_batch, HashedBatch, get_batch_url
from mwmbl.database import Database
from mwmbl.indexdb import BatchInfo, BatchStatus, IndexDatabase
from mwmbl.indexer.index import tokenize_document
from mwmbl.indexer.paths import INDEX_PATH
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


def run():
    date_str = str(date.today())
    users = get_user_id_hashes_for_date(date_str)
    print("Users", users)
    with Database() as db:
        index_db = IndexDatabase(db.connection)
        index_db.create_tables()
        for user in users:
            batches = get_batches_for_date_and_user(date_str, user)
            print("Batches", batches)
            batch_urls = [get_batch_url(batch_id, date_str, user) for batch_id in batches["batch_ids"]]
            infos = [BatchInfo(url, user, BatchStatus.REMOTE) for url in batch_urls]
            index_db.record_batches(infos)


def index_batches(index_path: str):
    nlp = spacy.load("en_core_web_sm")
    with TinyIndex(Document, index_path, 'w') as indexer:
        for batch_id in batch_ids["batch_ids"]:
            start = datetime.now()
            batch_dict = get_batch_from_id(date_str, user, batch_id)
            get_batch_time = datetime.now()
            print("Get batch time", get_batch_time - start)
            batch = HashedBatch.parse_obj(batch_dict['batch'])
            create_historical_batch(batch)
            create_historical_time = datetime.now()
            print("Create historical time", create_historical_time - get_batch_time)

            for item in batch.items:
                if item.content is None:
                    continue

                page = tokenize_document(item.url, item.content.title, item.content.extract, 1, nlp)
                for token in page.tokens:
                    indexer.index(token, Document(url=page.url, title=page.title, extract=page.extract, score=page.score))
            tokenize_time = datetime.now()
            print("Tokenize time", tokenize_time - create_historical_time)


if __name__ == '__main__':
    run()
