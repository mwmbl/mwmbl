"""
Export the list of unique URLs to a SQLite file for analysis/evaluation.
"""
import sqlite3

from mwmbl.indexer.paths import URLS_PATH
from mwmbl.tinysearchengine.app import get_config_and_index


def create_database():
    with sqlite3.connect(URLS_PATH) as connection:
        connection.execute("""
            CREATE TABLE urls (url TEXT PRIMARY KEY)
        """)


def get_url_batches():
    config, index = get_config_and_index()
    for page_num in range(config.index_config.num_pages):
        if page_num % 1000 == 0:
            print("Processing page", page_num)
        page = index.get_page(page_num)
        if page is None:
            continue
        yield [url for title, url, extract in page]


def run():
    create_database()
    url_batches = get_url_batches()

    with sqlite3.connect(URLS_PATH) as connection:
        for url_batch in url_batches:
            parameters = [(url,) for url in url_batch]
            connection.executemany("""
            INSERT OR IGNORE INTO urls VALUES (?)
            """, parameters)


if __name__ == '__main__':
    run()
