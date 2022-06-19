"""
Database interface for batches of crawled data.
"""
from dataclasses import dataclass
from enum import Enum

from psycopg2.extras import execute_values

from mwmbl.tinysearchengine.indexer import Document


class BatchStatus(Enum):
    REMOTE = 0    # The batch only exists in long term storage
    LOCAL = 1     # We have a copy of the batch locally in Postgresql
    INDEXED = 2   # The batch has been indexed and the local data has been deleted


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
            score FLOAT NOT NULL
        )
        """

        with self.connection.cursor() as cursor:
            cursor.execute(batches_sql)
            cursor.execute(documents_sql)

    def record_batches(self, batch_infos: list[BatchInfo]):
        sql = """
        INSERT INTO batches (url, user_id_hash, status) values %s
        ON CONFLICT (url) DO NOTHING 
        """

        data = [(info.url, info.user_id_hash, info.status.value) for info in batch_infos]

        with self.connection.cursor() as cursor:
            execute_values(cursor, sql, data)

    def get_batches_by_status(self, status: BatchStatus) -> list[BatchInfo]:
        sql = """
        SELECT * FROM batches WHERE status = %(status)s LIMIT 1000
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'status': status.value})
            results = cursor.fetchall()
            return [BatchInfo(url, user_id_hash, status) for url, user_id_hash, status in results]

    def queue_documents(self, documents: list[Document]):
        sql = """
        INSERT INTO documents (url, title, extract, score)
        VALUES %s
        ON CONFLICT (url) DO NOTHING
        """

        sorted_documents = sorted(documents, key=lambda x: x.url)
        data = [(document.url, document.title, document.extract, document.score) for document in sorted_documents]

        with self.connection.cursor() as cursor:
            execute_values(cursor, sql, data)
