"""Seed crawls: crawling outwards from the pages Staan found that the index lacked.

Combined Search with crawl=true hands the Staan results the index did not already hold to
a background task (mwmbl.background.seed_crawl), which crawls them and follows their links,
breadth first, until it has fetched SEED_CRAWL_MAX_PAGES pages. Links are only followed to
the domains Staan returned for the query, so a crawl stays on the sites the query was about.

The crawl goes a round at a time, one URL per domain per round and at least
SEED_CRAWL_DOMAIN_DELAY_SECONDS apart, so no site is fetched from faster than that however
much of the frontier it holds. Each round is indexed as it finishes, so what a crawl has
added is visible while it is still running.

What it added is recorded in Redis against the user and the query, where
GET /api/v2/combined-search/new-pages reads it, and counted on the user for their stats.
"""

import hashlib
import json
import time
from collections import deque
from datetime import datetime, timezone
from logging import getLogger

from django.conf import settings
from django.db.models import F
from django_redis import get_redis_connection

from mwmbl.crawler.retrieve import crawl_batch
from mwmbl.indexer.index_batches import index_new_documents
from mwmbl.models import MwmblUser
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import find_blacklisted_urls
from mwmbl.utils import bare_host

logger = getLogger(__name__)

STATUS_CRAWLING = "crawling"
STATUS_DONE = "done"


def _record_key(user_id: int, query: str) -> str:
    # Hashed so that a user's private query is not spelled out in the key space.
    query_hash = hashlib.sha256(query.encode()).hexdigest()
    return f"seed-crawl:{user_id}:{query_hash}"


def _pages_key(user_id: int, query: str) -> str:
    return f"{_record_key(user_id, query)}:pages"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_seed_crawl(user_id: int, query: str, index_pages: list[Document], staan_results: list[Document]):
    """Record a seed crawl as started, returning the seed URLs and domains to crawl it with.

    None when there is nothing to crawl - every Staan result is already in the index - or
    when this user's crawl for this query is still running, which a repeated search must
    not start again.
    """
    blacklisted_urls = find_blacklisted_urls(staan_results)
    allowed = [document for document in staan_results if document.url not in blacklisted_urls]
    indexed_urls = {document.url for document in index_pages}
    seed_urls = [document.url for document in allowed if document.url not in indexed_urls]
    if not seed_urls:
        return None

    redis = get_redis_connection("default")
    record_key = _record_key(user_id, query)
    if redis.hget(record_key, "status") == STATUS_CRAWLING.encode():
        return None

    ttl = settings.SEED_CRAWL_RECORD_TTL_SECONDS
    pipeline = redis.pipeline()
    pipeline.delete(record_key, _pages_key(user_id, query))
    pipeline.hset(
        record_key,
        mapping={"status": STATUS_CRAWLING, "query": query, "started_at": _now(), "pages_crawled": 0},
    )
    pipeline.expire(record_key, ttl)
    pipeline.execute()

    domains = sorted({bare_host(document.url) for document in allowed})
    return seed_urls, domains


def run_seed_crawl(user_id: int, query: str, seed_urls: list[str], domains: list[str], index_path: str) -> None:
    redis = get_redis_connection("default")
    record_key = _record_key(user_id, query)
    pages_key = _pages_key(user_id, query)
    ttl = settings.SEED_CRAWL_RECORD_TTL_SECONDS

    for num_crawled, documents in crawl_within_domains(seed_urls, set(domains), redis):
        # The seeds were missing from the index when the user searched, but Combined Search
        # has since written Staan's snippets of them, so the index alone no longer says so.
        new_urls = index_new_documents(documents, index_path) | set(seed_urls)
        new_documents = [document for document in documents if document.url in new_urls]
        logger.info("Seed crawl for user %d added %d new pages", user_id, len(new_documents))

        pipeline = redis.pipeline()
        pipeline.hincrby(record_key, "pages_crawled", num_crawled)
        if new_documents:
            pages = [{"url": doc.url, "title": doc.title, "extract": doc.extract} for doc in new_documents]
            pipeline.rpush(pages_key, *[json.dumps(page) for page in pages])
            pipeline.expire(pages_key, ttl)
        pipeline.execute()

        if new_documents:
            MwmblUser.objects.filter(id=user_id).update(
                seed_search_pages_indexed=F("seed_search_pages_indexed") + len(new_documents)
            )

    pipeline = redis.pipeline()
    pipeline.hset(record_key, mapping={"status": STATUS_DONE, "finished_at": _now()})
    pipeline.expire(record_key, ttl)
    pipeline.execute()


def crawl_within_domains(seed_urls: list[str], domains: set[str], redis):
    """Crawl breadth first from the seeds, staying within the domains given.

    Yields, for each round, how many URLs it fetched and the documents it got from them.
    """
    frontiers: dict[str, deque[str]] = {}
    seen_urls = set()

    def enqueue(url: str) -> None:
        domain = bare_host(url)
        if url in seen_urls or domain not in domains:
            return
        seen_urls.add(url)
        frontiers.setdefault(domain, deque()).append(url)

    for url in seed_urls:
        enqueue(url)

    max_pages = settings.SEED_CRAWL_MAX_PAGES
    num_crawled = 0
    last_round_started = None
    while num_crawled < max_pages:
        batch = [frontier.popleft() for frontier in frontiers.values() if frontier][: max_pages - num_crawled]
        if not batch:
            return

        if last_round_started is not None:
            elapsed = time.monotonic() - last_round_started
            time.sleep(max(0.0, settings.SEED_CRAWL_DOMAIN_DELAY_SECONDS - elapsed))
        last_round_started = time.monotonic()

        results = crawl_batch(batch, settings.SEED_CRAWL_THREADS, 0, redis)
        num_crawled += len(batch)

        documents = []
        for result in results:
            content = result["content"]
            if content is None:
                continue
            if content["title"]:
                documents.append(
                    Document(
                        content["title"],
                        result["url"],
                        content["extract"],
                        last_crawled=result["timestamp"] // 1000,
                    )
                )
            for link in content["links"] + content["extra_links"]:
                enqueue(link)
        yield len(batch), documents


def get_seed_crawl(user_id: int, query: str) -> dict | None:
    """The record of this user's seed crawl for this query, or None if there is none."""
    redis = get_redis_connection("default")
    record = redis.hgetall(_record_key(user_id, query))
    if not record:
        return None
    fields = {key.decode(): value.decode() for key, value in record.items()}
    pages = [json.loads(page) for page in redis.lrange(_pages_key(user_id, query), 0, -1)]
    return {
        "query": fields["query"],
        "status": fields["status"],
        "started_at": fields["started_at"],
        "finished_at": fields.get("finished_at"),
        "pages_crawled": int(fields["pages_crawled"]),
        "pages": pages,
    }
