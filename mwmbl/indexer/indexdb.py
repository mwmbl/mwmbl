"""
Database interface for batches of crawled data.
"""
from dataclasses import dataclass
from enum import Enum

from psycopg2.extras import execute_values


class BatchStatus(Enum):
    REMOTE = 0    # The batch only exists in long term storage
    LOCAL = 1     # We have a copy of the batch locally in Postgresql
    INDEXED = 2


class DocumentStatus(Enum):
    NEW = 0
    PREPROCESSING = 1


@dataclass
class BatchInfo:
    url: str
    user_id_hash: str
    status: BatchStatus


class IndexDatabase:
    def __init__(self, connection):
        self.connection = connection

    def create_tables(self):
        batches_sql = """
        CREATE TABLE IF NOT EXISTS batches (
            url VARCHAR PRIMARY KEY,
            user_id_hash VARCHAR NOT NULL,
            status INT NOT NULL
        )
        """

        documents_sql = """
        CREATE TABLE IF NOT EXISTS documents (
            url VARCHAR PRIMARY KEY,
            title VARCHAR NOT NULL,
            extract VARCHAR NOT NULL,
            score FLOAT NOT NULL,
            status INT NOT NULL
        )
        """

        document_pages_sql = """
        CREATE TABLE IF NOT EXISTS document_pages (
            url VARCHAR NOT NULL,
            page INT NOT NULL
        ) 
        """

        document_pages_index_sql = """
        CREATE INDEX IF NOT EXISTS document_pages_page_index ON document_pages (page)
        """

        with self.connection.cursor() as cursor:
            cursor.execute(batches_sql)
            cursor.execute(documents_sql)
            cursor.execute(document_pages_sql)
            cursor.execute(document_pages_index_sql)

    def record_batches(self, batch_infos: list[BatchInfo]):
        sql = """
        INSERT INTO batches (url, user_id_hash, status) values %s
        ON CONFLICT (url) DO NOTHING 
        """

        data = [(info.url, info.user_id_hash, info.status.value) for info in batch_infos]

        with self.connection.cursor() as cursor:
            execute_values(cursor, sql, data)

    def get_batches_by_status(self, status: BatchStatus, num_batches=1000) -> list[BatchInfo]:
        sql = """
        SELECT * FROM batches WHERE status = %(status)s LIMIT %(num_batches)s
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'status': status.value, 'num_batches': num_batches})
            results = cursor.fetchall()
            return [BatchInfo(url, user_id_hash, status) for url, user_id_hash, status in results]

    def update_batch_status(self, batch_urls: list[str], status: BatchStatus):
        sql = """
        UPDATE batches SET status = %(status)s
        WHERE url IN %(urls)s
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'status': status.value, 'urls': tuple(batch_urls)})
