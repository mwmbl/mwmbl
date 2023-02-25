"""
Database storing info on URLs
"""
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from logging import getLogger

from psycopg2.extras import execute_values

from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.settings import CORE_DOMAINS
# Client has one hour to crawl a URL that has been assigned to them, or it will be reassigned
from mwmbl.utils import batch

REASSIGN_MIN_HOURS = 5
BATCH_SIZE = 100
MAX_URLS_PER_TOP_DOMAIN = 100
MAX_TOP_DOMAINS = 500
MAX_OTHER_DOMAINS = 50000


logger = getLogger(__name__)


class URLStatus(Enum):
    """
    URL state update is idempotent and can only progress forwards.
    """
    NEW = 0                   # One user has identified this URL
    QUEUED = 5                # The URL has been queued for crawling
    ASSIGNED = 10             # The crawler has given the URL to a user to crawl
    ERROR_TIMEOUT = 20        # Timeout while retrieving
    ERROR_404 = 30            # 404 response
    ERROR_OTHER = 40          # Some other error
    ERROR_ROBOTS_DENIED = 50  # Robots disallow this page
    CRAWLED = 100             # At least one user has crawled the URL


@dataclass
class FoundURL:
    url: str
    user_id_hash: str
    score: float
    status: URLStatus
    timestamp: datetime


class URLDatabase:
    def __init__(self, connection):
        self.connection = connection

    def create_tables(self):
        logger.info("Creating URL tables")

        sql = """
        CREATE TABLE IF NOT EXISTS urls (
            url VARCHAR PRIMARY KEY,
            status INT NOT NULL DEFAULT 0,
            user_id_hash VARCHAR NOT NULL,
            score FLOAT NOT NULL DEFAULT 1,
            updated TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            # cursor.execute(index_sql)
            # cursor.execute(view_sql)

    def update_found_urls(self, found_urls: list[FoundURL]) -> list[FoundURL]:
        if len(found_urls) == 0:
            return []

        get_urls_sql = """
          SELECT url FROM urls
          WHERE url in %(urls)s
        """

        lock_urls_sql = """
          SELECT url FROM urls
          WHERE url in %(urls)s
          FOR UPDATE SKIP LOCKED
        """

        insert_sql = f"""
         INSERT INTO urls (url, status, user_id_hash, score, updated) values %s
         ON CONFLICT (url) DO UPDATE SET
           status = GREATEST(urls.status, excluded.status),
           user_id_hash = CASE
             WHEN urls.status > excluded.status THEN urls.user_id_hash ELSE excluded.user_id_hash
           END,
           score = urls.score + excluded.score,
           updated = CASE
             WHEN urls.status > excluded.status THEN urls.updated ELSE excluded.updated
           END
        RETURNING url, user_id_hash, score, status, updated
        """

        input_urls = [x.url for x in found_urls]
        assert len(input_urls) == len(set(input_urls))

        with self.connection as connection:
            with connection.cursor() as cursor:
                logger.info(f"Input URLs: {len(input_urls)}")
                cursor.execute(get_urls_sql, {'urls': tuple(input_urls)})
                existing_urls = {x[0] for x in cursor.fetchall()}
                new_urls = set(input_urls) - existing_urls

                cursor.execute(lock_urls_sql, {'urls': tuple(input_urls)})
                locked_urls = {x[0] for x in cursor.fetchall()}

                urls_to_insert = new_urls | locked_urls
                logger.info(f"URLs to insert: {len(urls_to_insert)}")

                if len(urls_to_insert) != len(input_urls):
                    print(f"Only got {len(urls_to_insert)} instead of {len(input_urls)} - {len(new_urls)} new")

                sorted_urls = sorted(found_urls, key=lambda x: x.url)
                data = [
                    (found_url.url, found_url.status.value, found_url.user_id_hash, found_url.score, found_url.timestamp)
                    for found_url in sorted_urls if found_url.url in urls_to_insert]

                logger.info(f"Data: {len(data)}")
                results = execute_values(cursor, insert_sql, data, fetch=True)
                logger.info(f"Results: {len(results)}")
                updated = [FoundURL(*result) for result in results]
                return updated

    def get_urls(self, status: URLStatus, num_urls: int) -> list[FoundURL]:
        sql = f"""
        SELECT url, status, user_id_hash, score, updated FROM urls
        WHERE status = %(status)s
        ORDER BY score DESC
        LIMIT %(num_urls)s
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'status': status.value, 'num_urls': num_urls})
            results = cursor.fetchall()

        return [FoundURL(url, user_id_hash, score, status, updated) for url, status, user_id_hash, score, updated in results]

    def get_url_scores(self, urls: list[str]) -> dict[str, float]:
        sql = f"""
        SELECT url, score FROM urls WHERE url IN %(urls)s
        """

        url_scores = {}
        for url_batch in batch(urls, 10000):
            with self.connection.cursor() as cursor:
                cursor.execute(sql, {'urls': tuple(url_batch)})
                results = cursor.fetchall()
                url_scores.update({result[0]: result[1] for result in results})

        return url_scores
