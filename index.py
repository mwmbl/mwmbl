"""
Create a search index
"""
import gzip
import json
import os
import sqlite3
from dataclasses import dataclass
from glob import glob
from itertools import chain, count, islice
from mmap import mmap, PROT_READ
from typing import List, Iterator
from urllib.parse import unquote

import bs4
import justext
import mmh3
from spacy.lang.en import English
from zstandard import ZstdCompressor, ZstdDecompressor, ZstdError

from paths import CRAWL_GLOB, INDEX_PATH

NUM_PAGES = 8192
PAGE_SIZE = 512

NUM_INITIAL_TOKENS = 50

HTTP_START = 'http://'
HTTPS_START = 'https://'
BATCH_SIZE = 100


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
class Document:
    url: str
    title: str


@dataclass
class TokenizedDocument(Document):
    tokens: List[str]


class TinyIndexBase:
    def __init__(self, num_pages, page_size):
        self.num_pages = num_pages
        self.page_size = page_size
        self.decompressor = ZstdDecompressor()
        self.mmap = None

    def retrieve(self, token):
        index = self._get_token_page_index(token)
        return self._get_page(index)

    def _get_token_page_index(self, token):
        token_hash = mmh3.hash(token, signed=False)
        return token_hash % self.num_pages

    def _get_page(self, i):
        """
        Get the page at index i, decompress and deserialise it using JSON
        """
        page_data = self.mmap[i * self.page_size:(i + 1) * self.page_size]
        try:
            decompressed_data = self.decompressor.decompress(page_data)
        except ZstdError:
            return None
        return json.loads(decompressed_data.decode('utf8'))


class TinyIndex(TinyIndexBase):
    def __init__(self, index_path, num_pages, page_size):
        super().__init__(num_pages, page_size)
        self.index_path = index_path
        self.index_file = open(self.index_path, 'rb')
        self.mmap = mmap(self.index_file.fileno(), 0, prot=PROT_READ)


class TinyIndexer(TinyIndexBase):
    def __init__(self, index_path, num_pages, page_size):
        super().__init__(num_pages, page_size)
        self.index_path = index_path
        self.compressor = ZstdCompressor()
        self.decompressor = ZstdDecompressor()
        self.index_file = None
        self.mmap = None

    def __enter__(self):
        self.create_if_not_exists()
        self.index_file = open(self.index_path, 'r+b')
        self.mmap = mmap(self.index_file.fileno(), 0)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.mmap.close()
        self.index_file.close()

    def index(self, documents: List[TokenizedDocument]):
        for document in documents:
            for token in document.tokens:
                self._index_document(document, token)

    def _index_document(self, document: Document, token: str):
        page_index = self._get_token_page_index(token)
        current_page = self._get_page(page_index)
        if current_page is None:
            current_page = []
        current_page.append([document.title, document.url])
        try:
            self._write_page(current_page, page_index)
        except ValueError:
            pass

    def _write_page(self, data, i):
        """
        Serialise the data using JSON, compress it and store it at index i.
        If the data is too big, it will raise a ValueError and not store anything
        """
        serialised_data = json.dumps(data)
        compressed_data = self.compressor.compress(serialised_data.encode('utf8'))
        page_length = len(compressed_data)
        if page_length > self.page_size:
            raise ValueError(f"Data is too big ({page_length}) for page size ({self.page_size})")
        padding = b'\x00' * (self.page_size - page_length)
        self.mmap[i * self.page_size:(i+1) * self.page_size] = compressed_data + padding

    def create_if_not_exists(self):
        if not os.path.isfile(self.index_path):
            file_length = self.num_pages * self.page_size
            with open(self.index_path, 'wb') as index_file:
                index_file.write(b'\x00' * file_length)

    def document_indexed(self, url):
        raise NotImplementedError()

    def get_num_tokens(self):
        raise NotImplementedError()

    def get_random_terms(self, n):
        raise NotImplementedError()


def run():
    indexer = TinyIndexer(INDEX_PATH, NUM_PAGES, PAGE_SIZE)
    indexer.create_if_not_exists()
    nlp = English()
    for path in glob(CRAWL_GLOB):
        print("Path", path)
        with gzip.open(path, 'rt') as html_file:
            url = html_file.readline().strip()
            content = html_file.read()

        if indexer.document_indexed(url):
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
        yield TokenizedDocument(tokens=list(tokens), url=url, title=title_cleaned)

        if i % 1000 == 0:
            print("Processed", i)


def grouper(n: int, iterator: Iterator):
    while True:
        chunk = tuple(islice(iterator, n))
        if not chunk:
            return
        yield chunk


def index_titles_and_urls(indexer: TinyIndexer, nlp, titles_and_urls):
    indexer.create_if_not_exists()

    pages = get_pages(nlp, titles_and_urls)
    for chunk in grouper(BATCH_SIZE, pages):
        indexer.index(list(chunk))


if __name__ == '__main__':
    run()
