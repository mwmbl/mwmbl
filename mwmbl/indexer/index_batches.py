"""
Index batches that are stored locally.
"""
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from logging import getLogger
from typing import Iterable
from urllib.parse import urlparse

import spacy

from mwmbl.crawler.batch import HashedBatch, Item
from mwmbl.crawler.urls import URLDatabase, URLStatus, FoundURL
from mwmbl.database import Database
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.index import tokenize_document
from mwmbl.indexer.indexdb import BatchStatus, IndexDatabase
from mwmbl.settings import UNKNOWN_DOMAIN_MULTIPLIER, SCORE_FOR_SAME_DOMAIN, SCORE_FOR_DIFFERENT_DOMAIN, \
    SCORE_FOR_ROOT_PATH
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

logger = getLogger(__name__)


EXCLUDED_DOMAINS = {'web.archive.org'}


def get_documents_from_batches(batches: Iterable[HashedBatch]) -> Iterable[tuple[str, str, str]]:
    for batch in batches:
        for item in batch.items:
            if item.content is not None:
                yield item.content.title, item.url, item.content.extract


def run(batch_cache: BatchCache, index_path: str):
    nlp = spacy.load("en_core_web_sm")
    with Database() as db:
        index_db = IndexDatabase(db.connection)

        logger.info("Getting local batches")
        batches = index_db.get_batches_by_status(BatchStatus.LOCAL, 10000)
        logger.info(f"Got {len(batches)} batch urls")
        if len(batches) == 0:
            return

        batch_data = batch_cache.get_cached([batch.url for batch in batches])
        logger.info(f"Got {len(batch_data)} cached batches")

        record_urls_in_database(batch_data.values())

        document_tuples = list(get_documents_from_batches(batch_data.values()))
        urls = [url for title, url, extract in document_tuples]

        logger.info(f"Got {len(urls)} document tuples")

        url_db = URLDatabase(db.connection)
        url_scores = url_db.get_url_scores(urls)

        logger.info(f"Got {len(url_scores)} scores")
        documents = [Document(title, url, extract, url_scores.get(url, 1.0)) for title, url, extract in document_tuples]

        page_documents = preprocess_documents(documents, index_path, nlp)
        index_pages(index_path, page_documents)
        logger.info("Indexed pages")
        index_db.update_batch_status([batch.url for batch in batches], BatchStatus.INDEXED)


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
            tokenized = tokenize_document(document.url, document.title, document.extract, document.score, nlp)
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


def record_urls_in_database(batches: Iterable[HashedBatch]):
    with Database() as db:
        url_db = URLDatabase(db.connection)
        url_scores = defaultdict(float)
        url_users = {}
        url_timestamps = {}
        url_statuses = defaultdict(lambda: URLStatus.NEW)
        for batch in batches:
            for item in batch.items:
                timestamp = get_datetime_from_timestamp(item.timestamp / 1000.0)
                url_timestamps[item.url] = timestamp
                url_users[item.url] = batch.user_id_hash
                if item.content is None:
                    url_statuses[item.url] = get_url_error_status(item)
                else:
                    url_statuses[item.url] = URLStatus.CRAWLED
                    crawled_page_domain = urlparse(item.url).netloc
                    score_multiplier = 1 if crawled_page_domain in DOMAINS else UNKNOWN_DOMAIN_MULTIPLIER
                    for link in item.content.links:
                        if parsed_link.netloc in EXCLUDED_DOMAINS:
                            continue

                        parsed_link = urlparse(link)
                        score = SCORE_FOR_SAME_DOMAIN if parsed_link.netloc == crawled_page_domain else SCORE_FOR_DIFFERENT_DOMAIN
                        url_scores[link] += score * score_multiplier
                        url_users[link] = batch.user_id_hash
                        url_timestamps[link] = timestamp
                        domain = f'{parsed_link.scheme}://{parsed_link.netloc}/'
                        url_scores[domain] += SCORE_FOR_ROOT_PATH * score_multiplier
                        url_users[domain] = batch.user_id_hash
                        url_timestamps[domain] = timestamp

        found_urls = [FoundURL(url, url_users[url], url_scores[url], url_statuses[url], url_timestamps[url])
                      for url in url_scores.keys() | url_statuses.keys()]

        url_db.update_found_urls(found_urls)


def get_datetime_from_timestamp(timestamp: float) -> datetime:
    batch_datetime = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=timestamp)
    return batch_datetime


# TODO: clean unicode at some point
def clean_unicode(s: str) -> str:
    return s.encode('utf-8', 'ignore').decode('utf-8')