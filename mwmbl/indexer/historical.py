from datetime import date, timedelta

from mwmbl.crawler.app import get_batches_for_date
from mwmbl.database import Database
from mwmbl.indexer.indexdb import BatchInfo, BatchStatus, IndexDatabase

DAYS = 20


def run():
    for day in range(DAYS):
        date_str = str(date.today() - timedelta(days=day))
        with Database() as db:
            index_db = IndexDatabase(db.connection)
            index_db.create_tables()
            batches = get_batches_for_date(date_str)
            batch_urls = batches['batch_urls']
            print("Historical batches for date", date_str, len(batch_urls))
            infos = [BatchInfo(url, get_user_id_hash_from_url(url), BatchStatus.REMOTE) for url in batch_urls]
            index_db.record_batches(infos)


def get_user_id_hash_from_url(url):
    return url.split('/')[9]


if __name__ == '__main__':
    run()
