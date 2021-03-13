"""
Create a search index
"""
import gzip
import sqlite3
from glob import glob

import bs4
import justext
from spacy.lang.en import English

from paths import CRAWL_GLOB, INDEX_PATH

NUM_INITIAL_TOKENS = 50


def is_content_token(nlp, token):
    lexeme = nlp.vocab[token.orth]
    return lexeme.is_alpha and not token.is_stop


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


def index(tokens, url, title):
    with sqlite3.connect(INDEX_PATH) as con:
        con.execute("""
            INSERT INTO pages (url, title)
            VALUES (?, ?)
        """, (url, title))

        result = con.execute("""
            SELECT last_insert_rowid()
        """)
        page_id = result.fetchone()[0]
        print("Created page with id", page_id)

        con.executemany("""
            INSERT INTO terms (term, page_id)
            VALUES (?, ?)
        """, [(term, page_id) for term in tokens])


def create_if_not_exists():
    con = sqlite3.connect(INDEX_PATH)
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


def page_indexed(url):
    con = sqlite3.connect(INDEX_PATH)
    result = con.execute("""
        SELECT EXISTS(SELECT 1 FROM pages WHERE url=?)
    """, (url,))
    value = result.fetchone()[0]
    return value == 1


def run():
    create_if_not_exists()
    nlp = English()
    for path in glob(CRAWL_GLOB):
        print("Path", path)
        with gzip.open(path, 'rt') as html_file:
            url = html_file.readline().strip()
            content = html_file.read()

        if page_indexed(url):
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
        index(tokens, url, title)


if __name__ == '__main__':
    run()
