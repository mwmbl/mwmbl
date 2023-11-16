"""
Index batches that are stored locally.
"""
from collections import defaultdict
from logging import getLogger
from typing import Collection, Iterable

import spacy
from mwmbl.indexer import process_batch
from spacy import Language

from mwmbl.crawler.batch import HashedBatch, Item
from mwmbl.crawler.urls import URLDatabase, URLStatus
from mwmbl.database import Database
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.index import tokenize_document
from mwmbl.indexer.indexdb import BatchStatus
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

logger = getLogger(__name__)


def get_documents_from_batches(batches: Collection[HashedBatch]) -> Iterable[tuple[str, str, str]]:
    for batch in batches:
        for item in batch.items:
            if item.content is not None:
                yield item.content.title, item.url, item.content.extract


def run(batch_cache: BatchCache, index_path: str):

    def process(batches: Collection[HashedBatch]):
        with Database() as db:
            nlp = spacy.load("en_core_web_sm")
            url_db = URLDatabase(db.connection)
            index_batches(batches, index_path, nlp, url_db)
            logger.info("Indexed pages")

    process_batch.run(batch_cache, BatchStatus.URLS_UPDATED, BatchStatus.INDEXED, process)


def index_batches(batch_data: Collection[HashedBatch], index_path: str, nlp: Language, url_db: URLDatabase):
    document_tuples = list(get_documents_from_batches(batch_data))
    urls = [url for title, url, extract in document_tuples]
    logger.info(f"Got {len(urls)} document tuples")
    url_scores = url_db.get_url_scores(urls)
    logger.info(f"Got {len(url_scores)} scores")
    documents = [Document(title, url, extract, url_scores.get(url, 1.0)) for title, url, extract in document_tuples]
    page_documents = preprocess_documents(documents, index_path, nlp)
    index_pages(index_path, page_documents)


def index_pages(index_path, page_documents):
    with TinyIndex(Document, index_path, 'w') as indexer:
        for page, documents in page_documents.items():
            new_documents = []
            existing_documents = indexer.get_page(page)
            seen_urls = set()
            seen_titles = set()
            sorted_documents = sorted(documents + existing_documents, key=lambda x: x.score, reverse=True)
            for document in sorted_documents:
                if document.title in seen_titles or document.url in seen_urls:
                    continue
                new_documents.append(document)
                seen_urls.add(document.url)
                seen_titles.add(document.title)
            indexer.store_in_page(page, new_documents)


def preprocess_documents(documents, index_path, nlp):
    page_documents = defaultdict(list)
    with TinyIndex(Document, index_path, 'w') as indexer:
        for document in documents:
            tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
            # logger.debug(f"Tokenized: {tokenized}")
            page_indexes = [indexer.get_key_page_index(token) for token in tokenized.tokens]
            for page in page_indexes:
                page_documents[page].append(document)
    print(f"Preprocessed for {len(page_documents)} pages")
    return page_documents


def get_url_error_status(item: Item):
    if item.status == 404:
        return URLStatus.ERROR_404
    if item.error is not None:
        if item.error.name == 'AbortError':
            return URLStatus.ERROR_TIMEOUT
        elif item.error.name == 'RobotsDenied':
            return URLStatus.ERROR_ROBOTS_DENIED
    return URLStatus.ERROR_OTHER
