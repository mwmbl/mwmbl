"""
Database storing info on URLs
"""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from psycopg2 import connect
from psycopg2.extras import execute_values


# Client has one hour to crawl a URL that has been assigned to them, or it will be reassigned
REASSIGN_MIN_HOURS = 1
BATCH_SIZE = 100


class URLStatus(Enum):
    """
    URL state update is idempotent and can only progress forwards.
    """
    NEW = 0         # One user has identified this URL
    CONFIRMED = 1   # A different user has identified the same URL
    ASSIGNED = 2    # The crawler has given the URL to a user to crawl
    CRAWLED = 3     # At least one user has crawled the URL


class URLDatabase:
    def __init__(self):
        self.connection = None

    def __enter__(self):
        self.connection = connect(user=os.environ["USER"])
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.connection.__exit__(exc_type, exc_val, exc_tb)
        self.connection.close()

    def create_tables(self):
        sql = """
        CREATE TABLE IF NOT EXISTS urls (
            url VARCHAR PRIMARY KEY,
            status INT NOT NULL DEFAULT 0,
            user_id_hash VARCHAR NOT NULL,
            score INT NOT NULL DEFAULT 1,
            updated TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql)

    def user_found_urls(self, user_id_hash: str, urls: list[str], timestamp: datetime):
        sql = f"""
        INSERT INTO urls (url, status, user_id_hash, score, updated) values %s
        ON CONFLICT (url) DO UPDATE SET 
          status = CASE
            WHEN excluded.status={URLStatus.NEW.value}
              AND excluded.user_id_hash != urls.user_id_hash
            THEN {URLStatus.CONFIRMED.value}
            ELSE {URLStatus.NEW.value}
          END,
          user_id_hash=excluded.user_id_hash,
          score=urls.score + 1,
          updated=excluded.updated
        """

        data = [(url, URLStatus.NEW.value, user_id_hash, 1, timestamp) for url in urls]

        with self.connection.cursor() as cursor:
            execute_values(cursor, sql, data)

    def user_crawled_urls(self, user_id_hash: str, urls: list[str], timestamp: datetime):
        sql = f"""
        INSERT INTO urls (url, status, user_id_hash, updated) values %s
        ON CONFLICT (url) DO UPDATE SET 
          status=excluded.status,
          user_id_hash=excluded.user_id_hash,
          updated=excluded.updated
        """

        data = [(url, URLStatus.CRAWLED.value, user_id_hash, timestamp) for url in urls]

        with self.connection.cursor() as cursor:
            execute_values(cursor, sql, data)

    def get_new_batch_for_user(self, user_id_hash: str):
        sql = f"""
        UPDATE urls SET status = {URLStatus.ASSIGNED.value}, user_id_hash = %(user_id_hash)s, updated = %(now)s
        WHERE url IN (
          SELECT url FROM urls
          WHERE status = {URLStatus.CONFIRMED.value} OR (
            status = {URLStatus.ASSIGNED.value} AND updated < %(min_updated_date)s
          )
          ORDER BY score DESC
          LIMIT {BATCH_SIZE}
          FOR UPDATE SKIP LOCKED
        )
        RETURNING url
        """

        now = datetime.utcnow()
        min_updated_date = now - timedelta(hours=REASSIGN_MIN_HOURS)
        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'user_id_hash': user_id_hash, 'min_updated_date': min_updated_date, 'now': now})
            results = cursor.fetchall()

        return [result[0] for result in results]


if __name__ == "__main__":
    with URLDatabase() as db:
        db.create_tables()
        # update_url_status(conn, [URLStatus("https://mwmbl.org", URLState.NEW, "test-user", datetime.now())])
        # db.user_found_urls("Test user", ["a", "b", "c"], datetime.utcnow())
        # db.user_found_urls("Another user", ["b", "c", "d"], datetime.utcnow())
        # db.user_crawled_urls("Test user", ["c"], datetime.utcnow())
        batch = db.get_new_batch_for_user('test user 4')
        print("Batch", len(batch), batch)
