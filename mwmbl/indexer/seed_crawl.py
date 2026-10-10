"""Seed crawls: crawling outwards from the pages Staan found that the index lacked.

Combined Search with crawl=true queues the Staan results the index did not already hold,
and a worker process of its own (run_seed_crawl_worker, started by mwmbl.main) crawls them
and follows their links, breadth first. Links are only followed to the domains Staan
returned for the query, so a crawl stays on the sites the query was about.

The crawl goes a round at a time, one URL per domain per round and at least
SEED_CRAWL_DOMAIN_DELAY_SECONDS apart, so no site is fetched from faster than that however
much of the frontier it holds. Each domain gives at most SEED_CRAWL_MAX_PAGES_PER_DOMAIN
pages, which also bounds a crawl to that many rounds - a few minutes - whatever the number
of domains. Each round is indexed as it finishes, so what a crawl has added is visible while
it is still running.

The crawls run in their own process rather than on the django-background-tasks queue: they
take minutes, and in series with the periodic tasks they would hold up the usage sync and
the Polar report. That process runs them one at a time, each user has at most one queued or
running, and at most SEED_CRAWL_MAX_QUEUED wait, so nobody can fill the queue.

What it added is recorded in Redis against the user and the query, where
GET /api/v2/combined-search/new-pages (and /new-pages/count) reads it, and counted on the user for their stats.
That record also says how far along the crawl is, and what it found on each of Staan's
domains, which are scored for crawlers as seed domains - see mwmbl.indexer.seed_domains.
"""

import hashlib
import json
import time
from collections import Counter, deque
from datetime import datetime, timezone
from enum import StrEnum
from http import HTTPStatus
from logging import getLogger
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.db import close_old_connections
from django.db.models import F
from django_redis import get_redis_connection

from mwmbl.crawler.retrieve import crawl_batch
from mwmbl.indexer.index_batches import index_new_documents
from mwmbl.indexer.seed_domains import new_page_score, record_seed_domain_crawls, register_seed_domains
from mwmbl.models import MwmblUser, SeedDomain
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import find_blacklisted_urls
from mwmbl.utils import bare_host

logger = getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_CRAWLING = "crawling"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class CrawlOutcome(StrEnum):
    """Why a search with crawl=true did or did not queue a crawl."""

    SCHEDULED = "scheduled"
    NO_RESULTS = "no_results"
    ALREADY_INDEXED = "already_indexed"
    ALREADY_RUNNING = "already_running"
    QUEUE_FULL = "queue_full"


QUEUE_KEY = "seed-crawl:queue"
# Under the Redis client's five-second socket timeout, which a longer blocking pop would trip.
QUEUE_POLL_SECONDS = 1


def _record_key(user_id: int, query: str) -> str:
    # Hashed so that a user's private query is not spelled out in the key space.
    query_hash = hashlib.sha256(query.encode()).hexdigest()
    return f"seed-crawl:{user_id}:{query_hash}"


def _pages_key(user_id: int, query: str) -> str:
    return f"{_record_key(user_id, query)}:pages"


def _counted_urls_key(user_id: int, query: str) -> str:
    return f"{_record_key(user_id, query)}:urls"


def _domain_pages_key(user_id: int, query: str) -> str:
    return f"{_record_key(user_id, query)}:domain-pages"


def _active_key(user_id: int) -> str:
    return f"seed-crawl:{user_id}:active"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_seed_crawl(
    user_id: int, query: str, index_pages: list[Document], staan_results: list[Document], new_staan_urls: set[str]
) -> CrawlOutcome:
    """Queue a seed crawl of the Staan results the index lacked, returning whether it did, or why not.

    The seeds are the results the query did not retrieve. Of those, only the ones in
    new_staan_urls - which Combined Search's own write of Staan's results found missing from
    the index - count as pages the crawl added; the rest the index already held.

    Nothing is queued when Staan returned nothing crawlable, when every Staan result is
    already in the index, when this user already has a crawl queued or running, or when the
    queue is full.
    """
    blacklisted_urls = find_blacklisted_urls(staan_results)
    allowed = [document for document in staan_results if document.url not in blacklisted_urls]
    indexed_urls = {document.url for document in index_pages}
    seed_urls = [document.url for document in allowed if document.url not in indexed_urls]
    if not allowed:
        return CrawlOutcome.NO_RESULTS
    if not seed_urls:
        return CrawlOutcome.ALREADY_INDEXED

    redis = get_redis_connection("default")
    # A slight overshoot from racing requests is harmless; the cap is there to stop one
    # user, or many, queueing hours of crawling.
    if redis.llen(QUEUE_KEY) >= settings.SEED_CRAWL_MAX_QUEUED:
        return CrawlOutcome.QUEUE_FULL
    # Set atomically, so a double click or a retried request cannot queue the crawl twice.
    # The expiry outlasts the longest wait plus the crawl, and frees the user should the
    # worker die mid-crawl.
    lock_seconds = settings.SEED_CRAWL_MAX_SECONDS * (settings.SEED_CRAWL_MAX_QUEUED + 1)
    if not redis.set(_active_key(user_id), query, nx=True, ex=lock_seconds):
        return CrawlOutcome.ALREADY_RUNNING

    record_key = _record_key(user_id, query)
    # Each domain with the number of Staan results it had, which the seed domains count.
    domains = dict(sorted(Counter(bare_host(document.url) for document in allowed).items()))
    new_seed_urls = [url for url in seed_urls if url in new_staan_urls]
    job = {
        "user_id": user_id,
        "query": query,
        "seed_urls": seed_urls,
        "new_seed_urls": new_seed_urls,
        "domains": domains,
    }
    # An earlier crawl's pages are kept: the new one adds to them, and the user's total
    # already includes them. Its progress and domains are this crawl's alone.
    record = {
        "status": STATUS_QUEUED,
        "query": query,
        "started_at": _now(),
        "rounds": 0,
        "domains": json.dumps(domains),
    }
    pipeline = redis.pipeline()
    pipeline.hset(record_key, mapping=record)
    pipeline.hsetnx(record_key, "pages_crawled", 0)
    pipeline.hdel(record_key, "finished_at", "crawl_started_at", "discovered")
    pipeline.delete(_domain_pages_key(user_id, query))
    pipeline.expire(record_key, settings.SEED_CRAWL_RECORD_TTL_SECONDS)
    pipeline.lpush(QUEUE_KEY, json.dumps(job))
    pipeline.execute()
    return CrawlOutcome.SCHEDULED


def get_active_seed_crawl_query(user_id: int) -> str | None:
    """The query of this user's queued or running crawl, or None if they have none."""
    query = get_redis_connection("default").get(_active_key(user_id))
    return None if query is None else query.decode()


def run_seed_crawl_worker() -> None:
    """Run queued seed crawls, one at a time, for ever."""
    index_path = str(Path(settings.DATA_PATH) / settings.INDEX_NAME)
    while True:
        run_next_seed_crawl(index_path)


def run_next_seed_crawl(index_path: str) -> bool:
    """Run the next queued seed crawl, if one arrives within QUEUE_POLL_SECONDS."""
    redis = get_redis_connection("default")
    popped = redis.brpop(QUEUE_KEY, timeout=QUEUE_POLL_SECONDS)
    if popped is None:
        return False
    # A worker that waits for hours between crawls would otherwise reuse a connection
    # Postgres has long since closed.
    close_old_connections()
    job = json.loads(popped[1])
    run_seed_crawl(job["user_id"], job["query"], job["seed_urls"], job["new_seed_urls"], job["domains"], index_path)
    return True


def run_seed_crawl(
    user_id: int, query: str, seed_urls: list[str], new_seed_urls: list[str], domains: dict[str, int], index_path: str
) -> None:
    redis = get_redis_connection("default")
    record_key = _record_key(user_id, query)
    redis.hset(record_key, mapping={"status": STATUS_CRAWLING, "crawl_started_at": _now()})
    try:
        discovered = register_seed_domains(user_id, domains)
        redis.hset(record_key, "discovered", json.dumps(sorted(discovered)))
        domain_pages = _crawl_and_record(
            user_id, query, seed_urls, set(new_seed_urls), list(domains), index_path, redis
        )
        # Only a finished crawl is recorded: a failed one's counts would understate its domains.
        record_seed_domain_crawls({domain: domain_pages[domain] for domain in domains})
        status = STATUS_DONE
    except Exception:
        # Recorded rather than retried: a retry would start again from the seeds, fetching
        # every site a second time. The worker carries on with the next crawl.
        logger.exception("Seed crawl for user %d failed", user_id)
        status = STATUS_FAILED
    finally:
        redis.delete(_active_key(user_id))

    pipeline = redis.pipeline()
    pipeline.hset(record_key, mapping={"status": status, "finished_at": _now()})
    pipeline.expire(record_key, settings.SEED_CRAWL_RECORD_TTL_SECONDS)
    pipeline.execute()


def _crawl_and_record(
    user_id: int, query: str, seed_urls: list[str], new_seed_urls: set[str], domains: list[str], index_path: str, redis
) -> Counter:
    """Crawl, index and record what is new, returning how many new pages each domain gave."""
    record_key = _record_key(user_id, query)
    pages_key = _pages_key(user_id, query)
    counted_urls_key = _counted_urls_key(user_id, query)
    domain_pages_key = _domain_pages_key(user_id, query)
    ttl = settings.SEED_CRAWL_RECORD_TTL_SECONDS
    domain_pages = Counter()

    for num_crawled, documents in crawl_within_domains(seed_urls, set(domains), redis):
        indexed = index_new_documents(documents, index_path)
        # Combined Search has since written Staan's snippets of the new seeds, so the index
        # alone no longer says they were new. Only those this write stored count.
        new_urls = indexed.new | (indexed.stored & new_seed_urls)
        candidates = [document for document in documents if document.url in new_urls]

        # A page is counted once per query, however many crawls of it find it new.
        pipeline = redis.pipeline()
        for document in candidates:
            pipeline.sadd(counted_urls_key, document.url)
        first_counted = pipeline.execute()
        new_documents = [document for document, added in zip(candidates, first_counted) if added]
        logger.info("Seed crawl for user %d added %d new pages", user_id, len(new_documents))
        round_domain_pages = Counter(bare_host(document.url) for document in new_documents)
        domain_pages.update(round_domain_pages)

        pipeline = redis.pipeline()
        pipeline.hincrby(record_key, "pages_crawled", num_crawled)
        pipeline.hincrby(record_key, "rounds", 1)
        if new_documents:
            pages = [{"url": doc.url, "title": doc.title, "extract": doc.extract} for doc in new_documents]
            pipeline.rpush(pages_key, *[json.dumps(page) for page in pages])
            pipeline.expire(pages_key, ttl)
            pipeline.expire(counted_urls_key, ttl)
            for domain, count in round_domain_pages.items():
                pipeline.hincrby(domain_pages_key, domain, count)
            pipeline.expire(domain_pages_key, ttl)
        pipeline.execute()

        if new_documents:
            MwmblUser.objects.filter(id=user_id).update(
                seed_search_pages_indexed=F("seed_search_pages_indexed") + len(new_documents)
            )
    return domain_pages


def crawl_within_domains(seed_urls: list[str], domains: set[str], redis):
    """Crawl breadth first from the seeds, staying within the domains given.

    Yields, for each round, how many URLs it fetched and the documents it got from them.
    """
    frontiers: dict[str, deque[str]] = {}
    seen_urls = set()
    enqueued_per_domain = Counter()
    max_pages_per_domain = settings.SEED_CRAWL_MAX_PAGES_PER_DOMAIN

    def enqueue(url: str) -> None:
        domain = bare_host(url)
        if url in seen_urls or domain not in domains or enqueued_per_domain[domain] >= max_pages_per_domain:
            return
        seen_urls.add(url)
        enqueued_per_domain[domain] += 1
        frontiers.setdefault(domain, deque()).append(url)

    for url in seed_urls:
        enqueue(url)

    def follow(link: str) -> None:
        # Query strings are where faceted search, sorting and calendars multiply one page
        # into thousands, which would spend a domain's budget on near-duplicates.
        if urlsplit(link).query:
            return
        enqueue(link)

    # The per-domain cap already bounds the rounds; this bounds a round's slow fetches.
    deadline = time.monotonic() + settings.SEED_CRAWL_MAX_SECONDS
    last_round_started = None
    while time.monotonic() < deadline:
        batch = [frontier.popleft() for frontier in frontiers.values() if frontier]
        if not batch:
            return

        if last_round_started is not None:
            elapsed = time.monotonic() - last_round_started
            time.sleep(max(0.0, settings.SEED_CRAWL_DOMAIN_DELAY_SECONDS - elapsed))
        last_round_started = time.monotonic()

        results = crawl_batch(batch, settings.SEED_CRAWL_THREADS, 0, redis)

        documents = []
        for result in results:
            content = result["content"]
            # An error page is not content, and its links are no lead to any.
            if content is None or result["status"] != HTTPStatus.OK:
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
                follow(link)
        yield len(batch), documents


def get_seed_crawl_summary(user_id: int, query: str) -> dict | None:
    """This user's seed crawl for this query without its pages, or None if there is none.

    Counts the pages rather than reading them, so it stays cheap for a crawl that has added
    thousands.
    """
    redis = get_redis_connection("default")
    record = redis.hgetall(_record_key(user_id, query))
    if not record:
        return None
    fields = {key.decode(): value.decode() for key, value in record.items()}
    domain_pages = {key.decode(): int(value) for key, value in redis.hgetall(_domain_pages_key(user_id, query)).items()}
    # Records written before seed domains existed have no domains; they expire within a week.
    domains = list(json.loads(fields.get("domains", "{}")))
    # Absent until the worker has started the crawl and registered its domains.
    discovered = json.loads(fields.get("discovered", "[]"))
    return {
        "query": fields["query"],
        "status": fields["status"],
        "started_at": fields["started_at"],
        "finished_at": fields.get("finished_at"),
        "pages_crawled": int(fields["pages_crawled"]),
        "pages_indexed": redis.llen(_pages_key(user_id, query)),
        "progress": _progress(fields),
        "domains": _domain_results(domains, discovered, domain_pages),
    }


def _progress(fields: dict[str, str]) -> float:
    """How far through its crawl a record is, from 0 to 1.

    A crawl ends at whichever comes first of its last round - at most one per page of a
    domain's budget - and its time limit, so its progress is the further along of the two.
    It jumps to 1 when the frontier runs dry before either.
    """
    if fields["status"] == STATUS_QUEUED:
        return 0.0
    if fields["status"] != STATUS_CRAWLING:
        return 1.0
    rounds_progress = int(fields["rounds"]) / settings.SEED_CRAWL_MAX_PAGES_PER_DOMAIN
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(fields["crawl_started_at"])
    time_progress = elapsed.total_seconds() / settings.SEED_CRAWL_MAX_SECONDS
    return min(1.0, max(rounds_progress, time_progress))


def _domain_results(domains: list[str], discovered: list[str], domain_pages: dict[str, int]) -> list[dict]:
    """What this crawl found on each of its domains, with their seed domain scores, best first.

    A domain only has scores once the worker has started the crawl and registered it.
    """
    seed_domains = SeedDomain.objects.filter(domain__in=domains).order_by("domain")
    results = [
        {
            "domain": seed_domain.domain,
            "newly_discovered": seed_domain.domain in discovered,
            "pages_indexed": domain_pages.get(seed_domain.domain, 0),
            "new_page_score": new_page_score(domain_pages.get(seed_domain.domain, 0)),
            "recent_new_page_score": seed_domain.new_page_score,
            "staan_results": seed_domain.staan_results,
            "score": seed_domain.score,
        }
        for seed_domain in seed_domains
    ]
    return sorted(results, key=lambda result: result["pages_indexed"], reverse=True)


def get_seed_crawl(user_id: int, query: str) -> dict | None:
    """This user's seed crawl for this query with the pages it added, or None if there is none."""
    summary = get_seed_crawl_summary(user_id, query)
    if summary is None:
        return None
    redis = get_redis_connection("default")
    pages = [json.loads(page) for page in redis.lrange(_pages_key(user_id, query), 0, -1)]
    return {**summary, "pages": pages}
