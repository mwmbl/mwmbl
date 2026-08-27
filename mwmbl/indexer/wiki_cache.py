"""Wikipedia search results, cached in the index rather than on disk.

get_wiki_results() used to cache through mwmbl.utils.request_cache, a requests-cache
*filesystem* session with a 10-week expiry. That writes one JSON file per response into
REQUEST_CACHE_PATH - the same volume as the index - with no LRU, no size cap and nothing
ever calling delete(expired=True), so it grew without bound; and because the stored JSON
holds the whole request, it wrote the raw user query to disk. The index is a fixed-size
preallocated file, so caching in it costs no additional disk at all.

A result set has up to three destinations, in increasing generality:

  * the *query-hash term* (always) - `#wiki-<hmac>`, which is the cache entry. Only a
    process holding SECRET_KEY can work out which term a query maps to.
  * the *real query terms* (gated) - so the document turns up as an ordinary result for
    anyone else's matching query, with source `wikipedia`.
  * the *standard indexing path* (see mwmbl.background), for URLs not seen before, filed
    under the document's own tokens exactly like a crawled page.

Only the first two involve the query at all, and the gate is what keeps the second one
anonymous: a document takes a real query term only if it contains every word of the
query, so the term->document link is derivable from the document itself and says nothing
about the query that produced it. Everything else is reachable only through the hash.

Be straight about what the hash does and does not do: it removes the query *text*, not the
fact that somebody searched for something in that area, since the entry still holds the
Wikipedia articles that were returned. That is strictly less than the disk cache gave away.
"""
import hashlib
import hmac
import json
import time
from logging import getLogger
from typing import Optional

import mmh3
import redis
from django.conf import settings

from mwmbl.indexer.index import document_token_set
from mwmbl.tinysearchengine.indexer import Document, DocumentState, TinyIndex
from mwmbl.tokenizer import get_bigrams, tokenize

logger = getLogger(__name__)


# Terms under this prefix are cache entries rather than words anybody searched for.
WIKI_CACHE_TERM_PREFIX = "#wiki-"
WIKI_CACHE_QUEUE_KEY = "wiki:index-queue"
WIKI_GENERAL_INDEXED_KEY = "wiki:general-indexed"

WIKI_STATES = {DocumentState.FROM_WIKI, DocumentState.FROM_WIKI_APPROVED}


_redis: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def wiki_cache_term(query: str) -> str:
    """The term a query's Wikipedia results are cached under.

    Keyed on SECRET_KEY rather than a bare digest: a plain hash of a one- or two-word
    query is trivially brute-forced from a wordlist, so an index dump would hand over the
    queries. Keyed, the mapping is not reproducible without the key. Rotating SECRET_KEY
    invalidates the cache, which is harmless - it is a cache.

    The query is normalised through tokenize() first, so "Python" and " python " share an
    entry.
    """
    normalised = " ".join(tokenize(query))
    digest = hmac.new(settings.SECRET_KEY.encode("utf8"), normalised.encode("utf8"), hashlib.sha256)
    return WIKI_CACHE_TERM_PREFIX + digest.hexdigest()[:16]


def is_fresh(document: Document, now: int) -> bool:
    """Whether a cached document is still within the TTL. Untimestamped means stale."""
    return (document.last_crawled is not None
            and now - document.last_crawled < settings.WIKI_CACHE_TTL_SECONDS)


def get_cached_wiki_results(tiny_index: TinyIndex, query: str, now: Optional[int] = None) -> list[Document]:
    """A query's cached Wikipedia results, or [] if we have none that are still fresh."""
    if not settings.WIKI_CACHE_ENABLED:
        return []

    now = int(time.time()) if now is None else now
    term = wiki_cache_term(query)
    cached = [document for document in tiny_index.retrieve(term)
              if document.term == term
              and document.state in WIKI_STATES
              and is_fresh(document, now)]

    # Order comes from the score, not from storage order. The queue is a Redis SET drained
    # with SPOP, so batch order is arbitrary; and under the hash term the write path scores
    # every document against a token none of them can match, so they all tie and keep
    # whatever order they arrived in.
    return sorted(cached, key=lambda document: -(document.score or 0.0))


def wiki_terms_for_documents(query: str, documents: list[Document]) -> list[Document]:
    """The per-term index copies to write for a set of Wikipedia results.

    Every usable document is filed under the query-hash term. A document is filed under
    the real query terms as well only if the query is short enough and the document
    contains all of its words - Wikipedia does spelling correction and partial matching,
    so a returned document need not contain the query words at all.

    last_crawled is left unset; the drain stamps it, which keeps identical results
    enqueued seconds apart on the same payload so the queue's SET dedupe still works.
    """
    usable = [document for document in documents if document.url and document.title]
    if not usable:
        return []

    cache_term = wiki_cache_term(query)
    copies = [_term_copy(document, cache_term) for document in usable]

    tokens = tokenize(query)
    if not tokens or len(tokens) > settings.WIKI_CACHE_MAX_ORGANIC_TERM_TOKENS:
        return copies

    organic_terms = tokens + get_bigrams(len(tokens), tokens)
    required = set(tokens)
    for document in usable:
        if not required <= document_token_set(document):
            continue
        copies += [_term_copy(document, term) for term in organic_terms]
    return copies


def _term_copy(document: Document, term: str) -> Document:
    return Document(
        title=document.title,
        url=document.url,
        extract=document.extract,
        score=document.score,
        term=term,
        state=DocumentState.FROM_WIKI,
    )


def _payload(document: Document) -> str:
    return json.dumps({
        "url": document.url,
        "title": document.title,
        "extract": document.extract,
        "score": document.score,
        "term": document.term,
    }, sort_keys=True)


def enqueue_wiki_results(query: str, documents: list[Document],
                         redis_client: Optional[redis.Redis] = None) -> int:
    """Queue a query's Wikipedia results to be written into the index. Never raises.

    The gate runs here, in the search worker, so the queue itself only ever holds terms
    derived from the documents plus the opaque hash - the query text never leaves the
    process. Losing an entry costs one re-fetch, which is why every failure is swallowed:
    nothing here may affect a search response.
    """
    if not settings.WIKI_CACHE_ENABLED:
        return 0

    copies = wiki_terms_for_documents(query, documents)
    if not copies:
        return 0

    payloads = list(dict.fromkeys(_payload(document) for document in copies))
    try:
        client = redis_client if redis_client is not None else get_redis()
        if client.scard(WIKI_CACHE_QUEUE_KEY) >= settings.WIKI_CACHE_MAX_QUEUE_SIZE:
            logger.warning("Wikipedia index queue is full (%d); dropping %d documents",
                           settings.WIKI_CACHE_MAX_QUEUE_SIZE, len(payloads))
            return 0
        return client.sadd(WIKI_CACHE_QUEUE_KEY, *payloads)
    except Exception:
        logger.warning("Could not enqueue %d Wikipedia results for indexing",
                       len(payloads), exc_info=True)
        return 0


def drain_wiki_queue(limit: int, redis_client: Optional[redis.Redis] = None) -> list[Document]:
    """Remove and return up to `limit` queued documents, stamped with the current time."""
    client = redis_client if redis_client is not None else get_redis()
    payloads = client.spop(WIKI_CACHE_QUEUE_KEY, limit) or []
    now = int(time.time())

    documents = []
    for payload in payloads:
        try:
            fields = json.loads(payload)
            if not fields["term"]:
                # Without a term there is no page to file it under, and
                # get_key_page_index would raise on the background task's next run.
                raise ValueError("queue entry has no term")
            documents.append(Document(
                title=fields["title"],
                url=fields["url"],
                extract=fields["extract"],
                score=fields["score"],
                term=fields["term"],
                state=DocumentState.FROM_WIKI,
                last_crawled=now,
            ))
        except (ValueError, KeyError, TypeError):
            logger.warning("Discarding unreadable Wikipedia queue entry: %r", payload)
    return documents


def wiki_queue_size(redis_client: Optional[redis.Redis] = None) -> int:
    client = redis_client if redis_client is not None else get_redis()
    return client.scard(WIKI_CACHE_QUEUE_KEY)


def _url_digest(url: str) -> str:
    return format(mmh3.hash64(url, signed=False)[0], 'x')


def unseen_wiki_urls(documents: list[Document], limit: int,
                     redis_client: Optional[redis.Redis] = None) -> list[Document]:
    """One document per URL not yet sent through the standard indexing path.

    Records only the URLs it returns, so a document dropped by `limit` is picked up by a
    later run rather than marked done and lost. Hashes the URLs rather than storing them
    to keep the set small.

    Being wrong is cheap in both directions - a false "seen" skips one document, a false
    "new" repeats ~57 page writes that combine_documents dedupes by URL anyway - which is
    also why it does not matter that prod Redis runs allkeys-lru and may evict this key.
    """
    by_url = {}
    for document in documents:
        if document.url and document.title and document.url not in by_url:
            by_url[document.url] = document
    if not by_url:
        return []

    urls = list(by_url)
    try:
        client = redis_client if redis_client is not None else get_redis()
        pipeline = client.pipeline()
        for url in urls:
            pipeline.sismember(WIKI_GENERAL_INDEXED_KEY, _url_digest(url))
        already_seen = pipeline.execute()

        unseen = [url for url, seen in zip(urls, already_seen) if not seen][:limit]
        if unseen:
            client.sadd(WIKI_GENERAL_INDEXED_KEY, *[_url_digest(url) for url in unseen])
    except Exception:
        logger.warning("Could not work out which Wikipedia URLs are new", exc_info=True)
        return []

    return [by_url[url] for url in unseen]


def drop_expired_wiki_cache(documents: list[Document], now: Optional[int] = None) -> list[Document]:
    """Cache entries past their TTL, dropped from a page that is being rewritten anyway.

    Pages are fixed-size and silently truncate their tail, so hash-term entries would
    otherwise pile up on whatever page they landed on and never leave. Doing it here makes
    it self-limiting: a page under write pressure cleans itself, and a page nobody rewrites
    is not under pressure. Only hash-term copies expire - copies filed under a document's
    own tokens are ordinary index content, not cache.
    """
    now = int(time.time()) if now is None else now
    return [document for document in documents if not _is_expired_cache_entry(document, now)]


def _is_expired_cache_entry(document: Document, now: int) -> bool:
    return (document.term is not None
            and document.term.startswith(WIKI_CACHE_TERM_PREFIX)
            and document.state == DocumentState.FROM_WIKI
            and not is_fresh(document, now))
