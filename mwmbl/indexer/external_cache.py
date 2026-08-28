"""Results from external search providers, cached in a TinyIndex of their own.

Wikipedia is the only provider using this today, but nothing here is Wikipedia-specific:
every entry is namespaced by its source, so a second provider (Staan, say) caches alongside
it in the same file without the two ever seeing each other's entries.

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
lets this module keep a negative-cache sentinel - see EXTERNAL_CACHE_EMPTY_URL.

Entries are keyed by a keyed hash of the query alone and carry their source as an int in
the document itself - a DocumentSource, exactly as the search index carries DocumentState.
What an entry stores in place of a score is the *rank* the provider gave it, because a
score belongs to whoever is ranking - see _entries_to_store.
Every provider's results for one query therefore land on one page: the second provider's
lookup reads a page the first one just warmed, rather than faulting in a second 4 KB page
from a 15 GB file. So the file says which provider each entry came from, and nothing about
which query. Be straight about what that does and does not do: it removes the query *text*,
not the fact that somebody searched in that area, since an entry still holds the results
that came back.
That is strictly less than the disk cache gave away.
"""
import hashlib
import hmac
import time
from logging import getLogger
from pathlib import Path
from typing import Optional

from django.conf import settings

from mwmbl.tinysearchengine.indexer import Document, DocumentSource, TinyIndex

logger = getLogger(__name__)


# A query a provider returned nothing for is stored as a single document under this URL, so
# that "we asked and there was nothing" stays distinguishable from "we never asked".
# Without it those queries are re-fetched forever. It is filtered out on read and never
# leaves this module.
#
# The empty string, because there will be a lot of these and an entry that holds no result
# should cost as close to nothing as an entry can. Nothing real can be mistaken for it:
# _entries_to_store drops any document without a url, so an empty url only ever means the
# sentinel.
EXTERNAL_CACHE_EMPTY_URL = ""


def external_cache_path() -> Path:
    return Path(settings.DATA_PATH) / settings.EXTERNAL_CACHE_INDEX_NAME


_cache_index: Optional[TinyIndex] = None
_cache_index_path: Optional[Path] = None


def get_cache_index() -> TinyIndex:
    """The read handle for this process, opened once and left open.

    Same lifetime as the main index in search_setup - mmap'd read-only for the life of the
    worker. Writers open their own short-lived 'w' handle instead, as every other writer in
    the codebase does; see store_external_results.

    Keyed on the path so that override_settings in the tests gets a fresh handle rather
    than whichever index the first test to run happened to open.
    """
    global _cache_index, _cache_index_path
    path = external_cache_path()
    if _cache_index is None or _cache_index_path != path:
        if _cache_index is not None:
            _cache_index.__exit__(None, None, None)
            _cache_index, _cache_index_path = None, None
        # Both globals are assigned only once __enter__ has returned. Assigning the index
        # first and the path after would, if __enter__ raised (the file removed between
        # __init__ and open, EMFILE, a failed mmap), leave a half-open index behind that
        # every later call would take the path-mismatch branch on and call __exit__ on -
        # an AttributeError on its None mmap, swallowed as a cache miss, for the life of
        # the worker. A failed open has to leave this exactly as it found it.
        index = TinyIndex(item_factory=Document, index_path=path)
        index.__enter__()
        _cache_index, _cache_index_path = index, path
    return _cache_index


def external_cache_term(query: str) -> str:
    """The term a query's cached results are stored under, for every provider alike.

    Deliberately not keyed on the source. All of a query's providers then share one page, so
    asking a second provider costs no extra page fault - it reads the page the first lookup
    just brought in. Which provider an entry belongs to is on the entry, as a DocumentSource
    int, and that is what the readers filter on. It is also what is_fresh would key a
    per-source TTL off, since a page's other entries tell it nothing through the term.

    Keyed on SECRET_KEY rather than a bare digest: a plain hash of a one- or two-word query
    is trivially brute-forced from a wordlist, so a dump of the file would hand over the
    queries. Keyed, the mapping is not reproducible without the key. Rotating SECRET_KEY
    invalidates the cache, which is harmless - it is a cache.

    Normalised on whitespace and case, so "Python" and " python " share an entry. The disk
    cache keyed on the request URL built from the raw string, so they did not.

    Deliberately not tokenize(): the document tokenizer is not a total function on queries.
    It drops the last two tokens from anything ending in "…" and returns [] for a query
    that is all whitespace, so "python asyncio…" and "python tutorial…" would both key on
    "python" and serve each other's results, and every whitespace-only query would share
    one entry. A key collision here is silent - one query is answered with another's
    results - so the normalisation has to be one that cannot merge two distinct queries.
    errors="ignore" on the encode keeps it total over strings the codec cannot represent
    (a lone surrogate), which is what clean_unicode() gave us inside tokenize: this runs
    outside the try in get_cached_external_results, so raising here would cost a search its
    results rather than a cache lookup.

    Bare, with no prefix on it: the file holds nothing but cache entries, so there is no
    real term for a namespace to keep it apart from. The prefix was needed when this lived
    in the search index, and every byte of it was stored on every entry.

    64 bits of digest is enough. Two distinct queries colliding here would serve one
    query's results for the other, so it is worth being explicit: at 30M live queries the
    birthday probability of *any* such pair existing is 2e-5. Widening the term is not free
    either - it is stored on every document, and 32 hex characters measured ~9% fewer
    entries per page - so the short term is the better trade. The collisions that do matter
    are page collisions, which are a different thing entirely; see EXTERNAL_CACHE_NUM_PAGES
    in settings_prod.
    """
    normalised = " ".join(query.split()).casefold()
    digest = hmac.new(settings.SECRET_KEY.encode("utf8"),
                      normalised.encode("utf8", errors="ignore"), hashlib.sha256)
    return digest.hexdigest()[:16]


def is_fresh(document: Document, now: int) -> bool:
    """Whether a cached document is still within its TTL. Untimestamped means stale.

    This runs over a page's other entries too, which belong to other queries and other
    providers, so it cannot recover the source from the term - document.source is the only
    thing that identifies them, and it is where a per-source TTL would key off.
    """
    ttl = (settings.EXTERNAL_CACHE_NEGATIVE_TTL_SECONDS if document.url == EXTERNAL_CACHE_EMPTY_URL
           else settings.EXTERNAL_CACHE_TTL_SECONDS)
    return document.last_crawled is not None and now - document.last_crawled < ttl


def get_cached_external_results(source: DocumentSource, query: str,
                                now: Optional[int] = None) -> Optional[list[Document]]:
    """A query's cached results from one provider.

    None means we have nothing for this (source, query) and should ask the provider. A list
    - possibly empty - means we already asked; an empty one is a query the provider had
    nothing for.
    """
    if not settings.EXTERNAL_CACHE_ENABLED:
        return None

    now = int(time.time()) if now is None else now
    term = external_cache_term(query)
    try:
        stored = get_cache_index().retrieve(term)
    except FileNotFoundError:
        logger.warning("No external results cache index at %s", external_cache_path())
        return None
    except Exception:
        # A cache we cannot read is a cache miss, never a failed search. retrieve() already
        # absorbs an unreadable page; this covers the file itself going away underneath us.
        logger.exception("Could not read the external results cache index")
        return None

    # retrieve() hands back the whole page, which is shared with every other query that
    # hashes to it, and it also lets through documents with no term at all. The term narrows
    # that to this query - across all providers, since the term does not name one - and the
    # source picks out the provider being asked about.
    entries = [document for document in stored
               if document.term == term and document.source == source and is_fresh(document, now)]
    if not entries:
        return None

    # Handed back in the provider's own rank order, with term set to the query and the
    # provider's state and source preserved, exactly as a live fetch builds them. The
    # hashed term is how the entry is stored, not something the ranker should ever see.
    #
    # score comes back None. What is stored is the rank (see _entries_to_store), and a rank
    # is not a score: turning one into the other needs the caller's scale, which the cache
    # does not know and must not guess. Callers score by position - get_wiki_results does.
    ranked = sorted(entries, key=lambda document: document.score or 0.0)
    return [Document(title=document.title, url=document.url, extract=document.extract,
                     score=None, term=query, state=document.state, source=document.source)
            for document in ranked if document.url != EXTERNAL_CACHE_EMPTY_URL]


def store_external_results(source: DocumentSource, query: str, documents: list[Document],
                           now: Optional[int] = None) -> None:
    """Store one provider's results for a query, replacing any existing entry for it.

    Never raises. A cache write that fails costs a re-fetch later; it must not cost a
    search its results.
    """
    if not settings.EXTERNAL_CACHE_ENABLED:
        return

    now = int(time.time()) if now is None else now
    term = external_cache_term(query)
    entries = _entries_to_store(source, term, documents, now)

    try:
        with TinyIndex(item_factory=Document, index_path=external_cache_path(), mode='w') as index:
            page_index = index.get_key_page_index(term)
            with index.page(page_index) as page:
                # Newest first. store() drops the tail that does not fit, so ordering by
                # age is what makes eviction fall on the oldest entries on the page.
                # Emphatically not the ranker's ordering, which would evict by relevance.
                # Dropping stale entries here is also the whole of expiry: a page under
                # write pressure cleans itself, and a page nobody rewrites is not under
                # pressure, so there is no sweep to schedule. Entries from other sources
                # are ordinary neighbours here - they compete for the page like any other.
                # Replaced entries are this query's *for this provider only*. Matching on
                # the term alone would be wrong now that providers share it: storing one
                # provider's results would silently drop every other provider's results for
                # the same query.
                kept = sorted(
                    (document for document in page.documents
                     if not (document.term == term and document.source == source)
                     and is_fresh(document, now)),
                    key=lambda document: -(document.last_crawled or 0))
                stored = page.store(entries + kept)
                if stored < len(entries):
                    logger.warning(
                        "External cache page %d held only %d of %d entries for one query",
                        page_index, stored, len(entries))
    except Exception:
        logger.exception("Could not store %s results in the external cache index", source.name)


def _entries_to_store(source: DocumentSource, term: str, documents: list[Document],
                      now: int) -> list[Document]:
    """The documents to write for this query, or the empty-result sentinel if there are none.

    Every entry carries its source, the sentinel included - an entry has to say which
    provider it came from without the key, and is_fresh has nothing else to go on. `state`
    is carried through from whatever the provider produced rather than stamped here.

    The score slot holds the document's *rank* in what the provider returned, not the score
    it was handed in with. A caller's score is on the caller's scale - get_wiki_results
    derives one from max_wiki_results - so storing it means an entry written by a caller
    asking for five results answers a caller asking for three with the wrong numbers, and
    that number is a feature of the LTR model. A rank is the provider's own statement about
    the result and means the same thing to every caller. Storing it explicitly rather than
    relying on list order also keeps the round trip from depending on the page preserving
    the order entries were written in.
    """
    usable = [document for document in documents if document.url and document.title]
    if not usable:
        return [Document(title="", url=EXTERNAL_CACHE_EMPTY_URL, extract="", score=0.0, term=term,
                         last_crawled=now, source=source)]

    return [Document(title=document.title, url=document.url, extract=document.extract,
                     score=float(rank), term=term, state=document.state, last_crawled=now,
                     source=source)
            for rank, document in enumerate(usable)]
