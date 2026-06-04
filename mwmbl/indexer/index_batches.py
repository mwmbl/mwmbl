"""
Index batches that are stored locally.
"""
import math
from collections import defaultdict, Counter
from datetime import datetime
from functools import reduce
from logging import getLogger
from typing import Collection, Iterable, Optional
from urllib.parse import unquote

from mwmbl.crawler.batch import HashedBatch, Item
from mwmbl.crawler.urls import URLStatus
from mwmbl.indexer import process_batch
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.index import tokenize_document, prepare_url_for_tokenizing
from mwmbl.indexer.indexdb import BatchStatus
from mwmbl.tinysearchengine.indexer import Document, TinyIndex, DocumentState, CURATED_STATES
from mwmbl.tinysearchengine.rank import score_result, DOCUMENT_FREQUENCIES, N_DOCUMENTS, HeuristicRanker
from mwmbl.tokenizer import tokenize, get_bigrams
from mwmbl.utils import add_term_infos

logger = getLogger(__name__)

MAX_USER_IDS = 2


def _merge_user_ids(
    existing: Optional[list[int]], incoming: Optional[list[int]]
) -> Optional[list[int]]:
    combined = list(existing or [])
    for uid in (incoming or []):
        if uid in combined:
            combined.remove(uid)
        combined.append(uid)
    return combined[-MAX_USER_IDS:] or None


def get_documents_from_batches(batches: Collection[HashedBatch]) -> Iterable[Document]:
    for batch in batches:
        for item in batch.items:
            if item.content is not None and not item.content.links_only:
                yield Document(
                    item.content.title, item.url, item.content.extract,
                    last_crawled=int(item.timestamp / 1000),
                )


def run(batch_cache: BatchCache, index_path: str):

    def process(batches: Collection[HashedBatch]):
        index_batches(batches, index_path)
        logger.info("Indexed pages")

    process_batch.run(batch_cache, BatchStatus.URLS_UPDATED, BatchStatus.INDEXED, process, 10000)


def get_url_score(url):
    # TODO: compute a proper score for each document
    return 1/len(url)


def index_batches(batch_data: Collection[HashedBatch], index_path: str) -> Counter:
    start_time = datetime.utcnow()
    documents = list(get_documents_from_batches(batch_data))
    end_time, new_page_doc_counts = index_documents(documents, index_path)
    logger.info(f"Indexing took {end_time - start_time}")
    return new_page_doc_counts


def index_documents(documents, index_path):
    page_documents = preprocess_documents(documents, index_path)
    new_page_doc_counts = index_pages(index_path, page_documents)
    end_time = datetime.utcnow()
    return end_time, new_page_doc_counts


def index_pages(index_path: str, page_documents: dict[int, list[Document]], mark_synced: bool = False) -> Counter:
    term_new_doc_counts = Counter()
    with TinyIndex(Document, index_path, 'w') as indexer:
        ranker = HeuristicRanker(indexer, None, score_threshold=float('-inf'))
        for page, documents in page_documents.items():
            existing_documents = indexer.get_page(page)
            combined_documents = combine_documents(existing_documents, documents, mark_synced, ranker)
            logger.info(f"Storing {len(combined_documents)} documents for page {page}, originally {len(existing_documents)}")
            indexer.store_in_page(page, combined_documents)

            term_new_doc_counts.update(document.term for document in combined_documents
                                       if document.state != DocumentState.SYNCED_WITH_MAIN_INDEX)
    return term_new_doc_counts


def _document_token_set(doc: Document) -> set[str]:
    """Unigram tokens of a document's title, URL and extract (no bigrams)."""
    prepared_url = prepare_url_for_tokenizing(unquote(doc.url))
    return (set(tokenize(doc.title))
            | set(tokenize(prepared_url))
            | set(tokenize(doc.extract)))


def index_results_against_query(documents: list[Document], query: str, index_path: str) -> int:
    """Index each document against the query unigrams/bigrams it matches.

    A query term matches a document when all of the term's words are present in
    the document's token set (unigram: the token; bigram: both words, in any
    order). Matching docs are stored against that term via index_pages(), which
    applies the normal combine/prioritise path. Returns the number of distinct
    URLs newly added to the index.

    The count is computed in the read pass, before combine/store, so a candidate
    later dropped by URL/title dedup or by the full-page trim is still counted;
    the figure is therefore a slight upper bound on what is persisted.
    """
    tokens = tokenize(query)
    if not tokens or not documents:
        return 0

    # term string -> the set of words that must all be present to match.
    query_terms: dict[str, frozenset[str]] = {t: frozenset((t,)) for t in tokens}
    for bigram in get_bigrams(len(tokens), tokens):
        query_terms[bigram] = frozenset(bigram.split())

    # Read pass: build per-page candidates and track which (term, url) are new.
    page_documents: dict[int, list[Document]] = defaultdict(list)
    new_urls: set[str] = set()
    with TinyIndex(Document, index_path, 'r') as indexer:
        existing_keys: dict[int, set[tuple]] = {}
        for doc in documents:
            if not (doc.url and doc.title and doc.extract):
                continue
            doc_tokens = _document_token_set(doc)
            for term, words in query_terms.items():
                if not (words <= doc_tokens):
                    continue
                page = indexer.get_key_page_index(term)
                page_documents[page].append(Document(
                    doc.title, doc.url, doc.extract,
                    term=term, last_crawled=doc.last_crawled,
                ))
                if page not in existing_keys:
                    existing_keys[page] = {(d.term, d.url) for d in indexer.get_page(page)}
                if (term, doc.url) not in existing_keys[page]:
                    new_urls.add(doc.url)

    if page_documents:
        index_pages(index_path, page_documents)  # reuse the existing write path
    return len(new_urls)


def combine_documents(existing_documents, documents, mark_synced, ranker):
    sorted_documents = sort_documents(documents, existing_documents, ranker)

    url_user_ids = {}
    url_last_crawled = {}
    for doc in sorted_documents:
        url_user_ids[doc.url] = _merge_user_ids(url_user_ids.get(doc.url), doc.user_ids)
        if doc.last_crawled is not None:
            url_last_crawled[doc.url] = max(url_last_crawled.get(doc.url, 0), doc.last_crawled)

    seen_urls = set()
    seen_titles = set()
    combined_documents = []
    for document in sorted_documents:
        if document.title in seen_titles or document.url in seen_urls:
            continue
        if mark_synced:
            document.state = DocumentState.SYNCED_WITH_MAIN_INDEX
        document.user_ids = url_user_ids.get(document.url)
        document.last_crawled = url_last_crawled.get(document.url)
        combined_documents.append(document)
        seen_urls.add(document.url)
        seen_titles.add(document.title)
    return combined_documents


def sort_documents(documents, all_existing_documents, ranker):
    curated_documents = [doc for doc in all_existing_documents if doc.state in CURATED_STATES]
    existing_documents = [doc for doc in all_existing_documents if doc.state not in CURATED_STATES]

    term_documents = defaultdict(list)

    for document in documents:
        if document.term is not None:
            term_documents[document.term].append(document)

    ordered_term_docs = defaultdict(list)
    for term, docs in term_documents.items():
        docs += [doc for doc in existing_documents if doc.term == term]
        ordered_docs = ranker.order_results(term.split(), docs, True)
        ordered_term_docs[term] = ordered_docs

    # Existing docs are already ordered
    other_terms = {doc.term for doc in existing_documents if doc.term not in ordered_term_docs}
    for doc in existing_documents:
        if doc.term in other_terms:
            ordered_term_docs[doc.term].append(doc)

    numbered_docs = [enumerate(docs) for docs in ordered_term_docs.values()]
    combined_docs = [doc for docs in numbered_docs for doc in docs]
    indexes, sorted_documents = zip(*sorted(combined_docs, key=lambda x: x[0]))
    return curated_documents + list(sorted_documents)


def preprocess_documents(documents, index_path):
    page_documents = defaultdict(list)
    with TinyIndex(Document, index_path, 'r') as indexer:
        for i, document in enumerate(documents):
            if i % 1000 == 0:
                logger.info(f"Preprocessing document {i} of {len(documents)}")

            tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
            for token in tokenized.tokens:
                page = indexer.get_key_page_index(token)
                term_document = Document(
                    document.title, document.url, document.extract,
                    term=token,
                    user_ids=document.user_ids,
                    last_crawled=document.last_crawled,
                )
                page_documents[page].append(term_document)
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
