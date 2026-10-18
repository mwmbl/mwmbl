"""
Write crawled documents into the index.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from logging import getLogger
from typing import Collection, Iterable, NamedTuple, Optional
from urllib.parse import unquote

from mwmbl.crawler.batch import HashedBatch, Item
from mwmbl.crawler.urls import URLStatus
from mwmbl.indexer.agreement import Agreement, find_agreements
from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist
from mwmbl.indexer.index import prepare_url_for_tokenizing, tokenize_document
from mwmbl.tinysearchengine.indexer import (
    CURATED_STATES,
    Document,
    DocumentSource,
    DocumentState,
    PageError,
    TinyIndex,
    cleaned_document,
)
from mwmbl.tinysearchengine.rank import HeuristicRanker
from mwmbl.tokenizer import get_bigrams, tokenize
from mwmbl.utils import get_domain

logger = getLogger(__name__)

MAX_USER_IDS = 2

# What an external result's source becomes in the search index. Only Staan's results are
# kept with one, under a source of their own - see DocumentSource.STAAN_INDEXED.
INDEXED_SOURCES = {DocumentSource.STAAN: DocumentSource.STAAN_INDEXED}


def _merge_user_ids(existing: Optional[list[int]], incoming: Optional[list[int]]) -> Optional[list[int]]:
    """The incoming ids go last, moved there if already present, and the oldest are dropped
    past MAX_USER_IDS."""
    combined = list(existing or [])
    for uid in incoming or []:
        if uid in combined:
            combined.remove(uid)
        combined.append(uid)
    return combined[-MAX_USER_IDS:] or None


def get_documents_from_batches(batches: Collection[HashedBatch]) -> Iterable[Document]:
    for batch in batches:
        for item in batch.items:
            if item.content is not None and not item.content.links_only:
                yield Document(
                    item.content.title,
                    item.url,
                    item.content.extract,
                    last_crawled=int(item.timestamp / 1000),
                )


def get_url_score(url):
    # TODO: compute a proper score for each document
    return 1 / len(url)


def index_batches(batch_data: Collection[HashedBatch], index_path: str) -> Counter:
    start_time = datetime.now(timezone.utc)
    documents = list(get_documents_from_batches(batch_data))
    indexed = index_documents(documents, index_path)
    logger.info(f"Indexing took {indexed.end_time - start_time}")
    return indexed.new_page_doc_counts


class IndexedDocuments(NamedTuple):
    end_time: datetime
    new_page_doc_counts: Counter
    agreements: set[Agreement]
    """Where an incoming document met another crawler's stored copy of its URL."""


def index_documents(documents, index_path) -> IndexedDocuments:
    """The common choke point every indexing path (the trusted-crawler POST /results
    endpoint, the standalone crawl tool) goes through, so
    this is where the blacklist is enforced. Crawling/link-discovery also check the
    blacklist (RedisURLQueue, update_urls.process_link) but only to stop *new* crawling -
    a /results submission can contain a blacklisted domain's pages regardless of whether that domain was ever handed out to be crawled (e.g. a
    browser-extension user organically visiting the site), so indexing needs its own check
    rather than relying on those upstream gates.

    The check reads the published snapshot rather than constructing the remote providers.
    POST /crawler/results calls this from a gunicorn worker, and the providers download
    tens of megabytes and hold ~156 MB of domain strings for the process's life - see
    blacklist_snapshot."""
    page_documents = _page_documents(documents, index_path)
    new_page_doc_counts = Counter()
    page_agreements = Counter()
    for page in _store_pages(index_path, page_documents):
        new_page_doc_counts.update(_new_terms(page.stored))
        page_agreements.update(page.agreements)
    end_time = datetime.now(timezone.utc)
    return IndexedDocuments(end_time, new_page_doc_counts, _one_agreement_per_url(page_agreements))


def _one_agreement_per_url(page_agreements: Counter) -> set[Agreement]:
    """The URL is on the page of each of its terms, and the pages need not all hold the same
    stored copy: two crawlers' versions have different terms, and which one a shared term's
    page keeps depends on rank. Keeping the copy most of its pages hold gives one agreement
    per submitted URL, so a URL with divergent copies is not counted several times over.

    The vote leaves out the crawl time gap: a page's stored crawl time is the latest of the
    copies merged on it, so one stored copy can carry different gaps on different pages."""
    copy_votes = Counter()
    agreement_by_copy = {}
    for agreement, count in page_agreements.items():
        copy = agreement._replace(last_crawled_delta=None)
        copy_votes[copy] += count
        agreement_by_copy.setdefault(copy, agreement)
    agreements_by_url = {}
    for copy, _ in copy_votes.most_common():
        agreements_by_url.setdefault(copy.url, agreement_by_copy[copy])
    return set(agreements_by_url.values())


class IndexedURLs(NamedTuple):
    stored: set[str]
    """URLs of the documents written, now on at least one of their pages."""
    new: set[str]
    """Of those, the URLs none of their pages held beforehand."""


def index_new_documents(documents, index_path) -> IndexedURLs:
    """index_documents, returning which URLs it stored and which of those the index lacked.

    A URL counts as new when none of the pages it is written to held it beforehand. Those
    are the pages its tokens hash to, so an earlier copy of the same page lives there too.
    Both are read under the page locks the write takes, so a document the store trimmed, or
    one the blacklist dropped, is in neither.
    """
    page_documents = _page_documents(documents, index_path)
    stored_urls = set()
    held_urls = set()
    for page in _store_pages(index_path, page_documents):
        stored_urls |= {document.url for document in page.stored}
        held_urls |= {document.url for document in page.existing}
    incoming_urls = {document.url for documents in page_documents.values() for document in documents}
    written_urls = stored_urls & incoming_urls
    return IndexedURLs(stored=written_urls, new=written_urls - held_urls)


def _page_documents(documents, index_path) -> dict[int, list[Document]]:
    documents = filter_blacklisted_documents(documents)
    # Cleaned here rather than at each of the paths above, for the same reason the
    # blacklist is: every one of them takes its text from somewhere outside, and a title or
    # an extract holding a character that cannot be stored costs the whole page it would be
    # written to - a page that is shared with documents from everywhere else.
    cleaned_documents = [cleaned_document(document) for document in documents]
    return preprocess_documents(cleaned_documents, index_path)


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
    for page in _store_pages(index_path, page_documents, mark_synced):
        term_new_doc_counts.update(_new_terms(page.stored))
    return term_new_doc_counts


def _new_terms(stored_documents: list[Document]) -> Iterable[str | None]:
    return (document.term for document in stored_documents if document.state != DocumentState.SYNCED_WITH_MAIN_INDEX)


class StoredPage(NamedTuple):
    stored: list[Document]
    """What the page now stores."""
    existing: list[Document]
    """What it held before."""
    agreements: set[Agreement]
    """Where an incoming document met another crawler's copy of its URL on the page."""


def _store_pages(index_path: str, page_documents: dict[int, list[Document]], mark_synced: bool = False):
    """Merge the documents into their pages, yielding a StoredPage for each page written."""
    with TinyIndex(Document, index_path, "w") as indexer:
        ranker = HeuristicRanker(indexer, None, score_threshold=float("-inf"))
        for page_index, documents in page_documents.items():
            try:
                with indexer.page(page_index) as page:
                    existing_documents = page.documents
                    # Before combining, which merges the user_ids and crawl times of the copies
                    # of a URL into whichever one it keeps, in place.
                    agreements = find_agreements(documents, existing_documents)
                    combined_documents = combine_documents(existing_documents, documents, mark_synced, ranker)
                    num_stored = page.store(combined_documents)
                    logger.info(
                        f"Storing {num_stored} of {len(combined_documents)} documents for "
                        f"page {page_index}, originally {len(existing_documents)}"
                    )
            except PageError:
                # One page we cannot safely write costs the documents bound for it, not the
                # rest of the batch. Letting it propagate would abort every remaining page,
                # and this runs inside POST /crawler/results and the standalone crawler's
                # indexing loop, where that means a 500 or a batch lost wholesale.
                logger.exception("Skipping index page %d", page_index)
                continue

            yield StoredPage(combined_documents[:num_stored], existing_documents, agreements)


def _document_token_set(doc: Document) -> set[str]:
    """Unigram tokens of a document's title, URL and extract (no bigrams)."""
    prepared_url = prepare_url_for_tokenizing(unquote(doc.url))
    return set(tokenize(doc.title)) | set(tokenize(prepared_url)) | set(tokenize(doc.extract))


def index_results_against_query(documents: list[Document], query: str, index_path: str) -> int:
    """index_new_results_against_query, returning how many URLs it added."""
    return len(index_new_results_against_query(documents, query, index_path))


def index_new_results_against_query(documents: list[Document], query: str, index_path: str) -> set[str]:
    """Index each document against the query unigrams/bigrams it matches.

    A query term matches a document when all of the term's words are present in
    the document's token set (unigram: the token; bigram: both words, in any
    order). Matching docs are stored against that term via index_pages(), which
    applies the normal combine/prioritise path. Returns the distinct URLs newly
    added to the index.

    The count is computed in the read pass, before combine/store, so a candidate
    later dropped by URL/title dedup or by the full-page trim is still counted;
    the figure is therefore a slight upper bound on what is persisted.
    """
    tokens = tokenize(query)
    if not tokens or not documents:
        return set()

    # term string -> the set of words that must all be present to match.
    query_terms: dict[str, frozenset[str]] = {t: frozenset((t,)) for t in tokens}
    for bigram in get_bigrams(len(tokens), tokens):
        query_terms[bigram] = frozenset(bigram.split())

    # Read pass: build per-page candidates and track which (term, url) are new.
    page_documents: dict[int, list[Document]] = defaultdict(list)
    new_urls: set[str] = set()
    with TinyIndex(Document, index_path, "r") as indexer:
        existing_keys: dict[int, set[tuple]] = {}
        for doc in documents:
            if not (doc.url and doc.title):
                continue
            doc_tokens = _document_token_set(doc)
            # This path does not go through index_documents, so it cleans its own text.
            cleaned = cleaned_document(doc)
            for term, words in query_terms.items():
                if not (words <= doc_tokens):
                    continue
                page = indexer.get_key_page_index(term)
                page_documents[page].append(
                    Document(
                        cleaned.title,
                        cleaned.url,
                        cleaned.extract,
                        term=term,
                        last_crawled=cleaned.last_crawled,
                        source=INDEXED_SOURCES.get(cleaned.source),
                    )
                )
                if page not in existing_keys:
                    try:
                        existing_keys[page] = {(d.term, d.url) for d in indexer.get_page(page)}
                    except PageError:
                        # An unlocked read, so most likely another writer mid-store. It only
                        # decides whether a URL counts as new; the write below takes the
                        # page's lock and reads it properly.
                        logger.warning("Could not read index page %d while counting new URLs", page)
                        existing_keys[page] = set()
                if (term, doc.url) not in existing_keys[page]:
                    new_urls.add(doc.url)

    if page_documents:
        index_pages(index_path, page_documents)  # reuse the existing write path
    return new_urls


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
        # The kept copy's ids merged last, so the last id is always the crawler whose text the
        # page holds - see mwmbl.indexer.agreement. Its own last id is its author by the same
        # rule, and a fresh submission carries only its submitter's. Only that last id is
        # meaningful: once the kept copy holds two, those of the copies it beats are dropped.
        document.user_ids = _merge_user_ids(url_user_ids.get(document.url), document.user_ids)
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
    with TinyIndex(Document, index_path, "r") as indexer:
        for i, document in enumerate(documents):
            if i % 1000 == 0:
                logger.info(f"Preprocessing document {i} of {len(documents)}")

            tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
            for token in tokenized.tokens:
                page = indexer.get_key_page_index(token)
                term_document = Document(
                    document.title,
                    document.url,
                    document.extract,
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
        if item.error.name == "AbortError":
            return URLStatus.ERROR_TIMEOUT
        elif item.error.name == "RobotsDenied":
            return URLStatus.ERROR_ROBOTS_DENIED
    return URLStatus.ERROR_OTHER
