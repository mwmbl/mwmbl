"""Wikipedia search results, cached in a TinyIndex of their own.

get_wiki_results() used to cache through mwmbl.utils.request_cache, a requests-cache
*filesystem* session rooted at REQUEST_CACHE_PATH - the same volume as the 400 GB index -
with no LRU, no size cap and nothing ever calling delete(expired=True), so it grew without
bound. Because the stored JSON holds the whole request, it also wrote the raw user query
to disk, where it sat for the length of the expiry.

This is a *separate* index file, not the search index. Caching into the search index was
tried in #357 and measured in #359: a cache entry and a real document compete for the same
4 KB page, and on a dense index storing one evicts about as much as it adds (-0.0054 NDCG
overall, -0.1927 on the queries where it fired). A file of its own also means a cache entry
can never surface in /raw or in the candidate pool for somebody else's query, which is what
lets this module keep a negative-cache sentinel - see WIKI_CACHE_EMPTY_URL.

Entries are keyed by a hash of the query, so the file holds no query text. Be straight
about what that does and does not do: it removes the query *text*, not the fact that
somebody searched in that area, since an entry still holds the articles that came back.
That is strictly less than the disk cache gave away.
"""
import hashlib
import hmac
import time
from logging import getLogger
from pathlib import Path
from typing import Optional

from django.conf import settings

from mwmbl.tinysearchengine.indexer import Document, DocumentState, TinyIndex
from mwmbl.tokenizer import tokenize

logger = getLogger(__name__)


# Terms under this prefix are cache entries rather than words anybody searched for.
WIKI_CACHE_TERM_PREFIX = "#wiki-"

# A query Wikipedia returned nothing for is stored as a single document under this URL, so
# that "we asked and there was nothing" stays distinguishable from "we never asked".
# Without it those queries are re-fetched forever. It is filtered out on read and never
# leaves this module.
WIKI_CACHE_EMPTY_URL = "mwmbl:wiki-cache-empty"


def wiki_cache_path() -> Path:
    return Path(settings.DATA_PATH) / settings.WIKI_CACHE_INDEX_NAME


_cache_index: Optional[TinyIndex] = None
_cache_index_path: Optional[Path] = None


def get_cache_index() -> TinyIndex:
    """The read handle for this process, opened once and left open.

    Same lifetime as the main index in search_setup - mmap'd read-only for the life of the
    worker. Writers open their own short-lived 'w' handle instead, as every other writer in
    the codebase does; see store_wiki_results.

    Keyed on the path so that override_settings in the tests gets a fresh handle rather
    than whichever index the first test to run happened to open.
    """
    global _cache_index, _cache_index_path
    path = wiki_cache_path()
    if _cache_index is None or _cache_index_path != path:
        if _cache_index is not None:
            _cache_index.__exit__(None, None, None)
        _cache_index = TinyIndex(item_factory=Document, index_path=path)
        _cache_index.__enter__()
        _cache_index_path = path
    return _cache_index


def wiki_cache_term(query: str) -> str:
    """The term a query's Wikipedia results are cached under.

    Keyed on SECRET_KEY rather than a bare digest: a plain hash of a one- or two-word query
    is trivially brute-forced from a wordlist, so a dump of the file would hand over the
    queries. Keyed, the mapping is not reproducible without the key. Rotating SECRET_KEY
    invalidates the cache, which is harmless - it is a cache.

    The query is normalised through tokenize() first, so "Python" and " python " share an
    entry. The disk cache keyed on the request URL built from the raw string, so they did
    not.
    """
    normalised = " ".join(tokenize(query))
    digest = hmac.new(settings.SECRET_KEY.encode("utf8"), normalised.encode("utf8"), hashlib.sha256)
    return WIKI_CACHE_TERM_PREFIX + digest.hexdigest()[:16]


def is_fresh(document: Document, now: int) -> bool:
    """Whether a cached document is still within its TTL. Untimestamped means stale."""
    ttl = (settings.WIKI_CACHE_NEGATIVE_TTL_SECONDS if document.url == WIKI_CACHE_EMPTY_URL
           else settings.WIKI_CACHE_TTL_SECONDS)
    return document.last_crawled is not None and now - document.last_crawled < ttl


def get_cached_wiki_results(query: str, now: Optional[int] = None) -> Optional[list[Document]]:
    """A query's cached Wikipedia results.

    None means we have nothing for this query and should ask Wikipedia. A list - possibly
    empty - means we already asked; an empty one is a query Wikipedia had nothing for.
    """
    if not settings.WIKI_CACHE_ENABLED:
        return None

    now = int(time.time()) if now is None else now
    term = wiki_cache_term(query)
    try:
        stored = get_cache_index().retrieve(term)
    except FileNotFoundError:
        logger.warning("No Wikipedia cache index at %s", wiki_cache_path())
        return None
    except Exception:
        # A cache we cannot read is a cache miss, never a failed search. retrieve() already
        # absorbs an unreadable page; this covers the file itself going away underneath us.
        logger.exception("Could not read the Wikipedia cache index")
        return None

    # retrieve() hands back the whole page, which is shared with every other query that
    # hashes to it, and it also lets through documents with no term at all.
    entries = [document for document in stored if document.term == term and is_fresh(document, now)]
    if not entries:
        return None

    # Handed back with term set to the query, exactly as a live fetch builds them. The
    # hashed term is how the entry is stored, not something the ranker should ever see.
    results = [Document(title=document.title, url=document.url, extract=document.extract,
                        score=document.score, term=query, state=document.state)
               for document in entries if document.url != WIKI_CACHE_EMPTY_URL]
    return sorted(results, key=lambda document: -(document.score or 0.0))


def store_wiki_results(query: str, documents: list[Document], now: Optional[int] = None) -> None:
    """Store a query's Wikipedia results, replacing any existing entry for it.

    Never raises. A cache write that fails costs a re-fetch later; it must not cost a
    search its results.
    """
    if not settings.WIKI_CACHE_ENABLED:
        return

    now = int(time.time()) if now is None else now
    term = wiki_cache_term(query)
    entries = _entries_to_store(term, documents, now)

    try:
        with TinyIndex(item_factory=Document, index_path=wiki_cache_path(), mode='w') as index:
            page_index = index.get_key_page_index(term)
            with index.page(page_index) as page:
                # Newest first. store() drops the tail that does not fit, so ordering by
                # age is what makes eviction fall on the oldest entries on the page.
                # Emphatically not the ranker's ordering, which would evict by relevance.
                # Dropping stale entries here is also the whole of expiry: a page under
                # write pressure cleans itself, and a page nobody rewrites is not under
                # pressure, so there is no sweep to schedule.
                kept = sorted(
                    (document for document in page.documents
                     if document.term != term and is_fresh(document, now)),
                    key=lambda document: -(document.last_crawled or 0))
                stored = page.store(entries + kept)
                if stored < len(entries):
                    logger.warning(
                        "Wiki cache page %d held only %d of %d entries for one query",
                        page_index, stored, len(entries))
    except Exception:
        logger.exception("Could not store Wikipedia results in the cache index")


def _entries_to_store(term: str, documents: list[Document], now: int) -> list[Document]:
    """The documents to write for this query, or the empty-result sentinel if there are none."""
    usable = [document for document in documents if document.url and document.title]
    if not usable:
        return [Document(title="", url=WIKI_CACHE_EMPTY_URL, extract="", score=0.0, term=term,
                         state=DocumentState.FROM_WIKI, last_crawled=now)]

    return [Document(title=document.title, url=document.url, extract=document.extract,
                     score=document.score, term=term, state=DocumentState.FROM_WIKI,
                     last_crawled=now)
            for document in usable]
