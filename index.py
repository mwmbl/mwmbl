"""
Create a search index
"""
import gzip
import sqlite3
from dataclasses import dataclass
from glob import glob
from itertools import chain, count, islice
from typing import List, Iterator
from urllib.parse import unquote

import bs4
import justext
from spacy.lang.en import English

from paths import CRAWL_GLOB, INDEX_PATH

NUM_INITIAL_TOKENS = 50

HTTP_START = 'http://'
HTTPS_START = 'https://'
BATCH_SIZE = 10000


def is_content_token(nlp, token):
    lexeme = nlp.vocab[token.orth]
    return (lexeme.is_alpha or lexeme.is_digit) and not token.is_stop


def tokenize(nlp, cleaned_text):
    tokens = nlp.tokenizer(cleaned_text)
    content_tokens = [token for token in tokens[:NUM_INITIAL_TOKENS]
                      if is_content_token(nlp, token)]
    lowered = {nlp.vocab[token.orth].text.lower() for token in content_tokens}
    return lowered


def clean(content):
    text = justext.justext(content, justext.get_stoplist("English"))
    pars = [par.text for par in text if not par.is_boilerplate]
    cleaned_text = ' '.join(pars)
    return cleaned_text


@dataclass
class Page:
    tokens: List[str]
    url: str
    title: str


class Indexer:
    def __init__(self, index_path):
        self.index_path = index_path

    def index(self, pages: List[Page]):
        with sqlite3.connect(self.index_path) as con:
            cursor = con.execute("""
                    SELECT max(id) FROM pages
                """)
            current_id = cursor.fetchone()[0]
            if current_id is None:
                first_page_id = 1
            else:
                first_page_id = current_id + 1

            page_ids = range(first_page_id, first_page_id + len(pages))
            urls_titles_ids = ((page.url, page.title, page_id)
                               for page, page_id in zip(pages, page_ids))
            con.executemany("""
                INSERT INTO pages (url, title, id)
                VALUES (?, ?, ?)
            """, urls_titles_ids)

            tokens = chain(*([(term, page_id) for term in page.tokens]
                             for page, page_id in zip(pages, page_ids)))
            con.executemany("""
                INSERT INTO terms (term, page_id)
                VALUES (?, ?)
            """, tokens)

    def create_if_not_exists(self):
        con = sqlite3.connect(self.index_path)
        con.execute("""
        CREATE TABLE IF NOT EXISTS pages (
          id INTEGER PRIMARY KEY,
          url TEXT UNIQUE,
          title TEXT
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS terms (
          term TEXT,
          page_id INTEGER 
        )
        """)

        con.execute("""
        CREATE INDEX IF NOT EXISTS term_index ON terms (term)
        """)

    def page_indexed(self, url):
        con = sqlite3.connect(self.index_path)
        result = con.execute("""
            SELECT EXISTS(SELECT 1 FROM pages WHERE url=?)
        """, (url,))
        value = result.fetchone()[0]
        return value == 1

    def get_num_tokens(self):
        con = sqlite3.connect(self.index_path)
        cursor = con.execute("""
            SELECT count(*) from terms
        """)
        num_terms = cursor.fetchone()[0]
        return num_terms


def run():
    indexer = Indexer(INDEX_PATH)
    indexer.create_if_not_exists()
    nlp = English()
    for path in glob(CRAWL_GLOB):
        print("Path", path)
        with gzip.open(path, 'rt') as html_file:
            url = html_file.readline().strip()
            content = html_file.read()

        if indexer.page_indexed(url):
            print("Page exists, skipping", url)
            continue

        cleaned_text = clean(content)
        try:
            title = bs4.BeautifulSoup(content, features="lxml").find('title').string
        except AttributeError:
            title = cleaned_text[:80]
        tokens = tokenize(nlp, cleaned_text)
        print("URL", url)
        print("Tokens", tokens)
        print("Title", title)
        indexer.index(tokens, url, title)


def prepare_url_for_tokenizing(url: str):
    if url.startswith(HTTP_START):
        url = url[len(HTTP_START):]
    elif url.startswith(HTTPS_START):
        url = url[len(HTTPS_START):]
    for c in '/._':
        if c in url:
            url = url.replace(c, ' ')
    return url


def get_pages(nlp, titles_and_urls):
    for i, (title_cleaned, url) in enumerate(titles_and_urls):
        title_tokens = tokenize(nlp, title_cleaned)
        prepared_url = prepare_url_for_tokenizing(unquote(url))
        url_tokens = tokenize(nlp, prepared_url)
        tokens = title_tokens | url_tokens
        yield Page(list(tokens), url, title_cleaned)

        if i % 1000 == 0:
            print("Processed", i)


def grouper(n: int, iterator: Iterator):
    while True:
        chunk = tuple(islice(iterator, n))
        if not chunk:
            return
        yield chunk


def index_titles_and_urls(indexer: Indexer, nlp, titles_and_urls):
    indexer.create_if_not_exists()

    pages = get_pages(nlp, titles_and_urls)
    for chunk in grouper(BATCH_SIZE, pages):
        indexer.index(list(chunk))


if __name__ == '__main__':
    run()
