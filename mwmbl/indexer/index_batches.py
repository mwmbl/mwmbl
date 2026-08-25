"""
Index batches that are stored locally.
"""
import math
from collections import defaultdict, Counter
from datetime import datetime
from functools import reduce
from logging import getLogger
from typing import Callable, Collection, Iterable, Optional
from urllib.parse import unquote

from mwmbl.crawler.batch import HashedBatch, Item
from mwmbl.crawler.urls import URLStatus
from mwmbl.indexer import process_batch
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist
from mwmbl.indexer.index import tokenize_document, prepare_url_for_tokenizing
from mwmbl.indexer.indexdb import BatchStatus
from mwmbl.tinysearchengine.indexer import Document, TinyIndex, DocumentState, CURATED_STATES
from mwmbl.tinysearchengine.rank import score_result, DOCUMENT_FREQUENCIES, N_DOCUMENTS, HeuristicRanker
from mwmbl.tokenizer import tokenize, get_bigrams
from mwmbl.utils import add_term_infos, get_domain

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
    """The common choke point every indexing path (offline batch processing, the
    trusted-crawler POST /results endpoint, the standalone crawl tool) goes through, so
    this is where the blacklist is enforced. Crawling/link-discovery also check the
    blacklist (RedisURLQueue, update_urls.process_link) but only to stop *new* crawling -
    a submitted batch or a direct /results submission can contain a blacklisted domain's
    pages regardless of whether that domain was ever handed out to be crawled (e.g. a
    browser-extension user organically visiting the site), so indexing needs its own check
    rather than relying on those upstream gates.

    The check reads the published snapshot rather than constructing the remote providers.
    POST /crawler/results calls this from a gunicorn worker, and the providers download
    tens of megabytes and hold ~156 MB of domain strings for the process's life - see
    blacklist_snapshot."""
    documents = filter_blacklisted_documents(documents)
    page_documents = preprocess_documents(documents, index_path)
    new_page_doc_counts = index_pages(index_path, page_documents)
    end_time = datetime.utcnow()
    return end_time, new_page_doc_counts


def filter_blacklisted_documents(documents: list[Document]) -> list[Document]:
    domains_by_url = {}
    for document in documents:
        try:
            domains_by_url[document.url] = get_domain(document.url)
        except ValueError:
            # Unparseable URL - keep it, matching rank.find_blacklisted_urls.
            continue

    blacklisted_domains = get_snapshot_blacklist().filter_blacklisted(domains_by_url.values())
    kept = []
    for document in documents:
        domain = domains_by_url.get(document.url)
        if domain in blacklisted_domains:
            logger.info(f"Skipping indexing for blacklisted domain {domain}: {document.url}")
            continue
        kept.append(document)
    return kept


def index_pages(index_path: str, page_documents: dict[int, list[Document]], mark_synced: bool = False) -> Counter:
    term_new_doc_counts = Counter()
    with TinyIndex(Document, index_path, 'w') as indexer:
        ranker = HeuristicRanker(indexer, None, score_threshold=float('-inf'))
        for page, documents in page_documents.items():
            def merge(existing_documents, documents=documents, page=page):
                combined = combine_documents(existing_documents, documents, mark_synced, ranker)
                logger.info(f"Storing {len(combined)} documents for page {page}, "
                            f"originally {len(existing_documents)}")
                return combined

            combined_documents = indexer.update_page(page, merge)
            term_new_doc_counts.update(document.term for document in combined_documents
                                       if document.state != DocumentState.SYNCED_WITH_MAIN_INDEX)
    return term_new_doc_counts


def _document_token_set(doc: Document) -> set[str]:
    """Unigram tokens of a document's title, URL and extract (no bigrams)."""
    prepared_url = prepare_url_for_tokenizing(unquote(doc.url))
    return (set(tokenize(doc.title))
            | set(tokenize(prepared_url))
            | set(tokenize(doc.extract)))


def index_results_against_query(documents: list[Document], query: str, index_path: str,
                                max_term_tokens: Optional[int] = None,
                                require_existing_term: bool = False,
                                term_exists: Optional[Callable[[str], bool]] = None,
                                state: Optional[DocumentState] = None,
                                keep_score: bool = False) -> int:
    """Index each document against the query unigrams/bigrams it matches.

    A query term matches a document when all of the term's words are present in
    the document's token set (unigram: the token; bigram: both words, in any
    order). Matching docs are stored against that term via index_pages(), which
    applies the normal combine/prioritise path. Returns the number of distinct
    URLs newly added to the index.

    The containment rule is what makes this safe to run on user queries: a document
    only takes a term whose words it already contains, so the term -> document link is
    derivable from the document itself and says nothing about the query that produced
    it. `max_term_tokens` caps how many words a stored term may have (so a long query is
    never written as one phrase), and `require_existing_term` restricts writes to terms
    the index already holds documents for, so nothing is stored that the corpus did not
    already contain - by default asking the index being written to, or `term_exists` when
    the corpus the caller means is larger than that (the evaluation's overlay index).
    All of these default to off, which is the behaviour Super Search has always had;
    mwmbl.tinysearchengine.wiki_index_cache passes them from settings.

    `keep_score` carries the incoming document's score onto the stored copies. It is off
    by default because a Super Search document's score is not comparable across sources,
    but a Wikipedia result's score *is* its rank in Wikipedia's own results - and the LTR
    model reads `score` as a feature, so dropping it makes every copy served from the
    index look worse to the ranker than the identical result fetched live.

    `state` is stamped on the stored copies. Wikipedia results keep FROM_WIKI so they are
    recognisable later - as the source shown to the user, and as the thing the
    from_wiki_only gate counts. It is never taken from the incoming document, whose
    `term` is the raw user query; every stored copy is rebuilt with a derived term.

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
    if max_term_tokens is not None:
        query_terms = {term: words for term, words in query_terms.items()
                       if len(words) <= max_term_tokens}
    if not query_terms:
        return 0

    # Read pass: build per-page candidates and track which (term, url) are new.
    page_documents: dict[int, list[Document]] = defaultdict(list)
    new_urls: set[str] = set()
    with TinyIndex(Document, index_path, 'r') as indexer:
        existing_keys: dict[int, set[tuple]] = {}

        def keys_for_page(page: int) -> set[tuple]:
            if page not in existing_keys:
                existing_keys[page] = {(d.term, d.url) for d in indexer.get_page(page)}
            return existing_keys[page]

        if require_existing_term:
            def in_this_index(term: str) -> bool:
                page = indexer.get_key_page_index(term)
                return any(t == term for t, _ in keys_for_page(page))

            check = term_exists if term_exists is not None else in_this_index
            query_terms = {term: words for term, words in query_terms.items() if check(term)}

        for doc in documents:
            if not (doc.url and doc.title):
                continue
            doc_tokens = _document_token_set(doc)
            for term, words in query_terms.items():
                if not (words <= doc_tokens):
                    continue
                page = indexer.get_key_page_index(term)
                page_documents[page].append(Document(
                    doc.title, doc.url, doc.extract,
                    score=doc.score if keep_score else None,
                    term=term, state=state, last_crawled=doc.last_crawled,
                ))
                if (term, doc.url) not in keys_for_page(page):
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
