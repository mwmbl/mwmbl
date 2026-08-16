"""The hand-off between the search path and the background index purge.

When retrieval filters a blacklisted document out of a result set, that document is still
sitting in the index and will be re-filtered on every future query that touches its page.
The search worker cannot remove it there and then - that is a read-modify-write of an
index page - so it drops it on this queue and a background task
(mwmbl.background.purge_blacklisted_from_queue) does the removal.

Two properties matter:

  * Enqueueing must never be able to affect a search response. Every Redis call here is
    wrapped, and a failure is logged and swallowed. Losing an entry is harmless: the
    document is still filtered out of the results, and the next query that surfaces it
    enqueues it again. The whole loop is self-healing, which is also why it is safe that
    production Redis runs allkeys-lru and can evict this key outright.
  * It must not grow without bound. A Redis SET dedupes identical payloads for free -
    the same popular blacklisted document surfacing on a thousand queries is one entry -
    and MAX_QUEUE_SIZE caps the pathological case where the purge task is not running.

The payload carries exactly the four fields tokenize_document() needs to work out which
index pages a document is filed under. They are read straight out of the index, so the
serialised JSON is byte-identical between retrievals and SET dedupe works on it.
"""
import json
import threading
import time
from collections import OrderedDict
from logging import getLogger
from typing import Iterable, Optional

import redis
from django.conf import settings

from mwmbl.tinysearchengine.indexer import Document

logger = getLogger(__name__)


PURGE_QUEUE_KEY = "blacklist:purge-queue"
MAX_QUEUE_SIZE = 100_000

# A blacklisted document stays in the index until the next purge run, so every query that
# surfaces it in the meantime tries to queue it again - two Redis round trips to re-add
# something already in the SET. This remembers what this process has queued recently and
# skips those, which matters most for exactly the documents that matter most: the ones
# turning up on popular queries. The TTL means a document whose purge failed or was lost
# is retried rather than suppressed forever.
_RECENTLY_ENQUEUED_TTL = 30 * 60
_MAX_RECENTLY_ENQUEUED = 10_000
_recently_enqueued: "OrderedDict[str, float]" = OrderedDict()
_recently_enqueued_lock = threading.Lock()


def _take_unqueued(payloads: list[str]) -> list[str]:
    """Filter out payloads this process queued recently, and record the rest."""
    now = time.monotonic()
    with _recently_enqueued_lock:
        while _recently_enqueued:
            payload, queued_at = next(iter(_recently_enqueued.items()))
            if now - queued_at < _RECENTLY_ENQUEUED_TTL:
                break
            _recently_enqueued.popitem(last=False)

        fresh = [p for p in payloads if p not in _recently_enqueued]
        for payload in fresh:
            _recently_enqueued[payload] = now
        while len(_recently_enqueued) > _MAX_RECENTLY_ENQUEUED:
            _recently_enqueued.popitem(last=False)
    return fresh


_redis: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def _payload(document: Document) -> str:
    return json.dumps({
        "url": document.url,
        "title": document.title,
        "extract": document.extract,
        "score": document.score,
    }, sort_keys=True)


def enqueue_for_purge(documents: Iterable[Document], redis_client: Optional[redis.Redis] = None) -> int:
    """Queue these documents for removal from the index. Returns the number of *new*
    entries added (already-queued duplicates do not count). Never raises."""
    payloads = _take_unqueued([_payload(document) for document in documents])
    if not payloads:
        return 0

    try:
        client = redis_client if redis_client is not None else get_redis()
        if client.scard(PURGE_QUEUE_KEY) >= MAX_QUEUE_SIZE:
            logger.warning("Blacklist purge queue is full (%d); dropping %d documents",
                           MAX_QUEUE_SIZE, len(payloads))
            return 0
        return client.sadd(PURGE_QUEUE_KEY, *payloads)
    except Exception:
        logger.warning("Could not enqueue %d documents for purging", len(payloads), exc_info=True)
        return 0


def drain_purge_queue(limit: int, redis_client: Optional[redis.Redis] = None) -> list[Document]:
    """Remove and return up to `limit` queued documents."""
    client = redis_client if redis_client is not None else get_redis()
    payloads = client.spop(PURGE_QUEUE_KEY, limit) or []

    documents = []
    for payload in payloads:
        try:
            fields = json.loads(payload)
            documents.append(Document(title=fields["title"], url=fields["url"],
                                      extract=fields["extract"], score=fields["score"]))
        except (ValueError, KeyError, TypeError):
            logger.warning("Discarding unreadable purge queue entry: %r", payload)
    return documents


def queue_size(redis_client: Optional[redis.Redis] = None) -> int:
    client = redis_client if redis_client is not None else get_redis()
    return client.scard(PURGE_QUEUE_KEY)
