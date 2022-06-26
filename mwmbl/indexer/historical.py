from datetime import date, datetime, timedelta

import spacy

from mwmbl.crawler.app import get_user_id_hashes_for_date, get_batches_for_date_and_user, get_batch_from_id, \
    create_historical_batch, get_batch_url
from mwmbl.crawler.batch import HashedBatch
from mwmbl.database import Database
from mwmbl.indexer.indexdb import BatchInfo, BatchStatus, IndexDatabase
from mwmbl.indexer.index import tokenize_document
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


DAYS = 10


def run():
    for day in range(DAYS):
        date_str = str(date.today() - timedelta(days=day))
        users = get_user_id_hashes_for_date(date_str)
        print(f"Got {len(users)} for day {date_str}")
        with Database() as db:
            index_db = IndexDatabase(db.connection)
            index_db.create_tables()
            for user in users:
                batches = get_batches_for_date_and_user(date_str, user)
                print("Historical batches for user", user, len(batches))
                batch_urls = [get_batch_url(batch_id, date_str, user) for batch_id in batches["batch_ids"]]
                infos = [BatchInfo(url, user, BatchStatus.REMOTE) for url in batch_urls]
                index_db.record_batches(infos)


if __name__ == '__main__':
    run()
