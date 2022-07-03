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

    def get_batches_by_status(self, status: BatchStatus) -> list[BatchInfo]:
        sql = """
        SELECT * FROM batches WHERE status = %(status)s LIMIT 1000
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'status': status.value})
            results = cursor.fetchall()
            return [BatchInfo(url, user_id_hash, status) for url, user_id_hash, status in results]

    def update_batch_status(self, batch_urls: list[str], status: BatchStatus):
        sql = """
        UPDATE batches SET status = %(status)s
        WHERE url IN %(urls)s
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'status': status.value, 'urls': tuple(batch_urls)})

    def queue_documents(self, documents: list[Document]):
        sql = """
        INSERT INTO documents (url, title, extract, score, status)
        VALUES %s
        ON CONFLICT (url) DO NOTHING
        """

        sorted_documents = sorted(documents, key=lambda x: x.url)
        data = [(document.url, document.title, document.extract, document.score, DocumentStatus.NEW.value)
                for document in sorted_documents]

        print("Queueing documents", len(data))
        with self.connection.cursor() as cursor:
            execute_values(cursor, sql, data)

    def get_documents_for_preprocessing(self):
        sql = f"""
        UPDATE documents SET status = {DocumentStatus.PREPROCESSING.value}
        WHERE url IN (
            SELECT url FROM documents
            WHERE status = {DocumentStatus.NEW.value}
            LIMIT 1000
            FOR UPDATE SKIP LOCKED
        )
        RETURNING url, title, extract, score
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            results = cursor.fetchall()
            return [Document(title, url, extract, score) for url, title, extract, score in results]

    def clear_documents_for_preprocessing(self) -> int:
        sql = f"""
        DELETE FROM documents WHERE status = {DocumentStatus.PREPROCESSING.value}
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            return cursor.rowcount

    def queue_documents_for_page(self, urls_and_page_indexes: list[tuple[str, int]]):
        sql = """
        INSERT INTO document_pages (url, page) values %s
        """

        print(f"Queuing {len(urls_and_page_indexes)} urls and page indexes")
        with self.connection.cursor() as cursor:
            execute_values(cursor, sql, urls_and_page_indexes)

    def get_queued_documents_for_page(self, page_index: int) -> list[Document]:
        sql = """
        SELECT d.url, title, extract, score
        FROM document_pages p INNER JOIN documents d ON p.url = d.url
        WHERE p.page = %(page_index)s
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'page_index': page_index})
            results = cursor.fetchall()
            return [Document(title, url, extract, score) for url, title, extract, score in results]

    def get_queued_pages(self) -> list[int]:
        sql = """
        SELECT DISTINCT page FROM document_pages ORDER BY page
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            results = cursor.fetchall()
            return [x[0] for x in results]

    def clear_queued_documents_for_page(self, page_index: int):
        sql = """
        DELETE FROM document_pages
        WHERE page = %(page_index)s
        """

        with self.connection.cursor() as cursor:
            cursor.execute(sql, {'page_index': page_index})
