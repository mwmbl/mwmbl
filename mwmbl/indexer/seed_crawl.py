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

Everything about a crawl is in one Redis hash, its record, which is read by
GET /api/v2/combined-search/new-pages (and /new-pages/count). That includes its frontier,
so a crawl is a series of rounds that any worker can pick up, rather than minutes of state
held in one process. A crawl queued or running is in READY_KEY, scored by when it was
queued, so the oldest is crawled to the end before the next starts. A worker claims it by
pushing its score out by SEED_CRAWL_LEASE_SECONDS, runs one round, and writes the round's
outcome together with the new frontier and the crawl's own score back - or, at the end, the
crawl's status - in one transaction. A worker
that dies mid-round never writes, so once its lease runs out the crawl is claimed again
from the round it was on; one that is merely slow is shut out by the claim's token, which
the next claim changes. So there is nothing to clean up after a worker, however it stopped.

The crawls run in their own process rather than on the django-background-tasks queue: they
take minutes, and in series with the periodic tasks they would hold up the usage sync and
the Polar report. One crawl runs at a time, each user has at most one
queued or running, and at most SEED_CRAWL_MAX_QUEUED are, so nobody can fill the queue.

The new pages a crawl added are counted on the user for their stats, and what it found on
each of Staan's domains is scored for crawlers as seed domains - see
mwmbl.indexer.seed_domains.
"""

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
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
from redis import WatchError
from redis.client import Pipeline

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
    ALREADY_CRAWLED = "already_crawled"
    QUEUE_FULL = "queue_full"


# The records of the crawls queued or running, scored by when each was queued, or while a
# worker holds one, by when its lease ends.
READY_KEY = "seed-crawl:ready"
# How long the worker waits before looking again when no crawl is due.
POLL_SECONDS = 1


def _record_key(user_id: int, query: str) -> str:
    # Hashed so that a user's private query is not spelled out in the key space.
    query_hash = hashlib.sha256(query.encode()).hexdigest()
    return f"seed-crawl:{user_id}:{query_hash}"


def _pages_key(record_key: str) -> str:
    return f"{record_key}:pages"


def _counted_urls_key(record_key: str) -> str:
    return f"{record_key}:urls"


def _domain_pages_key(record_key: str) -> str:
    return f"{record_key}:domain-pages"


def _active_key(user_id: int) -> str:
    # Named apart from the expiring lock that held the user's query before crawls were
    # leased, so those left behind by the deploy cannot block anyone.
    return f"seed-crawl:{user_id}:active-crawl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Frontier:
    """What is left of a crawl: the URLs still to fetch from each domain, and its counts.

    A domain is dropped once SEED_CRAWL_MAX_STALE_PAGES_PER_DOMAIN fetches in a row from it
    have added nothing new, so a crawl ends when there is nothing left for it to find rather
    than when it has fetched every domain's budget.
    """

    domains: set[str]
    queues: dict[str, list[str]] = field(default_factory=dict)
    seen_urls: set[str] = field(default_factory=set)
    enqueued_per_domain: Counter = field(default_factory=Counter)
    stale_fetches_per_domain: Counter = field(default_factory=Counter)

    @classmethod
    def from_seeds(cls, seed_urls: list[str], domains: set[str]) -> "Frontier":
        frontier = cls(domains)
        for url in seed_urls:
            frontier.enqueue(url)
        return frontier

    @classmethod
    def from_json(cls, data: str) -> "Frontier":
        fields = json.loads(data)
        return cls(
            set(fields["domains"]),
            fields["queues"],
            set(fields["seen_urls"]),
            Counter(fields["enqueued_per_domain"]),
            Counter(fields["stale_fetches_per_domain"]),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "domains": sorted(self.domains),
                "queues": self.queues,
                "seen_urls": sorted(self.seen_urls),
                "enqueued_per_domain": self.enqueued_per_domain,
                "stale_fetches_per_domain": self.stale_fetches_per_domain,
            }
        )

    def enqueue(self, url: str) -> None:
        domain = bare_host(url)
        if (
            url in self.seen_urls
            or domain not in self.domains
            or self.enqueued_per_domain[domain] >= settings.SEED_CRAWL_MAX_PAGES_PER_DOMAIN
        ):
            return
        self.seen_urls.add(url)
        self.enqueued_per_domain[domain] += 1
        self.queues.setdefault(domain, []).append(url)

    def follow(self, link: str) -> None:
        # Query strings are where faceted search, sorting and calendars multiply one page
        # into thousands, which would spend a domain's budget on near-duplicates.
        if urlsplit(link).query:
            return
        self.enqueue(link)

    def next_batch(self) -> list[str]:
        """Take the next URL of each domain that has one."""
        return [queue.pop(0) for queue in self.queues.values() if queue]

    def record_round(self, batch: list[str], new_urls: set[str]) -> None:
        """Note which of a round's URLs gave new pages, dropping the domains that have run dry."""
        for url in batch:
            domain = bare_host(url)
            if url in new_urls:
                self.stale_fetches_per_domain[domain] = 0
                continue
            self.stale_fetches_per_domain[domain] += 1
            if self.stale_fetches_per_domain[domain] >= settings.SEED_CRAWL_MAX_STALE_PAGES_PER_DOMAIN:
                self.queues[domain].clear()


def crawl_round(frontier: Frontier, batch: list[str], redis) -> list[Document]:
    """Fetch a round's URLs, adding their links to the frontier and returning their documents."""
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
            frontier.follow(link)
    return documents


def start_seed_crawl(
    user_id: int, query: str, index_pages: list[Document], staan_results: list[Document], new_staan_urls: set[str]
) -> CrawlOutcome:
    """Queue a seed crawl of the Staan results the index lacked, returning whether it did, or why not.

    The seeds are the results the query did not retrieve. Of those, only the ones in
    new_staan_urls - which Combined Search's own write of Staan's results found missing from
    the index - count as pages the crawl added; the rest the index already held.

    Nothing is queued when Staan returned nothing crawlable, when every Staan result is
    already in the index, when this user has already crawled the query, when they already
    have a crawl queued or running, or when the queue is full.
    """
    blacklisted_urls = find_blacklisted_urls(staan_results)
    allowed = [document for document in staan_results if document.url not in blacklisted_urls]
    indexed_urls = {document.url for document in index_pages}
    seed_urls = [document.url for document in allowed if document.url not in indexed_urls]
    if not allowed:
        return CrawlOutcome.NO_RESULTS
    if not seed_urls:
        return CrawlOutcome.ALREADY_INDEXED

    # Each domain with the number of Staan results it had, which the seed domains count.
    staan_results_per_domain = Counter(bare_host(document.url) for document in allowed)
    domains = dict(sorted(staan_results_per_domain.items()))
    new_seed_urls = [url for url in seed_urls if url in new_staan_urls]
    return queue_seed_crawl(user_id, query, seed_urls, new_seed_urls, domains)


# Queues a crawl, unless the user already has one queued or running, has crawled the query
# already, or the queue is full. One script, so the user's active crawl, the ready set and
# the record can never disagree. A failed earlier crawl's pages are kept: the new one adds
# to them, and the user's total already includes them. Its progress and domains are this
# crawl's alone. The token is bumped so that no worker still holding an earlier crawl of the
# query can write to this one.
_QUEUE_SCRIPT = """
local record, active, ready, domain_pages = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
if redis.call('ZSCORE', ready, record) then
    return 'already_running'
end
if redis.call('HGET', record, 'status') == 'done' then
    return 'already_crawled'
end
if redis.call('EXISTS', active) == 1 then
    return 'already_running'
end
if redis.call('ZCARD', ready) >= tonumber(ARGV[1]) then
    return 'queue_full'
end
redis.call('HSET', record, unpack(ARGV, 4))
redis.call('HSETNX', record, 'pages_crawled', 0)
redis.call('HDEL', record, 'finished_at', 'crawl_started_at', 'discovered', 'attempts', 'last_round_started')
redis.call('HINCRBY', record, 'token', 1)
redis.call('EXPIRE', record, ARGV[3])
redis.call('DEL', domain_pages)
redis.call('SET', active, record)
redis.call('ZADD', ready, ARGV[2], record)
return 'scheduled'
"""


def queue_seed_crawl(
    user_id: int, query: str, seed_urls: list[str], new_seed_urls: list[str], domains: dict[str, int]
) -> CrawlOutcome:
    """Queue a crawl from these seeds within these domains, unless the user may not have one."""
    record_key = _record_key(user_id, query)
    queued_at = time.time()
    record = {
        "status": STATUS_QUEUED,
        "user_id": user_id,
        "query": query,
        "started_at": _now(),
        "queued_at": queued_at,
        "rounds": 0,
        "domains": json.dumps(domains),
        "new_seed_urls": json.dumps(new_seed_urls),
        "frontier": Frontier.from_seeds(seed_urls, set(domains)).to_json(),
    }
    fields = [item for pair in record.items() for item in pair]
    outcome = get_redis_connection("default").eval(
        _QUEUE_SCRIPT,
        4,
        record_key,
        _active_key(user_id),
        READY_KEY,
        _domain_pages_key(record_key),
        settings.SEED_CRAWL_MAX_QUEUED,
        queued_at,
        settings.SEED_CRAWL_RECORD_TTL_SECONDS,
        *fields,
    )
    return CrawlOutcome(outcome.decode())


def get_active_seed_crawl_query(user_id: int) -> str | None:
    """The query of this user's queued or running crawl, or None if they have none."""
    redis = get_redis_connection("default")
    record_key = redis.get(_active_key(user_id))
    if record_key is None:
        return None
    query = redis.hget(record_key, "query")
    return query.decode()


def run_seed_crawl_worker() -> None:
    """Run queued seed crawls' rounds, oldest due first, for ever.

    Stopping it at any point, however abruptly, loses at most the round it was on, which the
    next claim of that crawl runs again.
    """
    index_path = str(Path(settings.DATA_PATH) / settings.INDEX_NAME)
    while True:
        if not run_next_seed_crawl_round(index_path):
            time.sleep(POLL_SECONDS)


# Claims the crawl due soonest, if one is due, by pushing its score out to when the lease
# ends, and returns its record's key and a fresh token. A crawl whose round has been claimed
# SEED_CRAWL_MAX_ATTEMPTS times without finishing is failed instead: the round may be what
# keeps killing its workers. The user's active crawl key is built here rather than passed
# in, as the crawl is only known once claimed.
_CLAIM_SCRIPT = """
local ready = KEYS[1]
local due = redis.call('ZRANGEBYSCORE', ready, '-inf', ARGV[1], 'LIMIT', 0, 1)
local record = due[1]
if not record then
    return false
end
local attempts = redis.call('HINCRBY', record, 'attempts', 1)
local token = redis.call('HINCRBY', record, 'token', 1)
if attempts > tonumber(ARGV[3]) then
    redis.call('HSET', record, 'status', 'failed', 'finished_at', ARGV[4])
    redis.call('HDEL', record, 'frontier')
    redis.call('ZREM', ready, record)
    redis.call('DEL', 'seed-crawl:' .. redis.call('HGET', record, 'user_id') .. ':active-crawl')
    return false
end
if redis.call('HGET', record, 'status') == 'queued' then
    redis.call('HSET', record, 'status', 'crawling', 'crawl_started_at', ARGV[4])
end
redis.call('ZADD', ready, tonumber(ARGV[1]) + tonumber(ARGV[2]), record)
return {record, token}
"""


def run_next_seed_crawl_round(index_path: str) -> bool:
    """Run a round of the crawl due soonest, returning whether one was due."""
    redis = get_redis_connection("default")
    claimed = redis.eval(
        _CLAIM_SCRIPT,
        1,
        READY_KEY,
        time.time(),
        settings.SEED_CRAWL_LEASE_SECONDS,
        settings.SEED_CRAWL_MAX_ATTEMPTS,
        _now(),
    )
    if claimed is None:
        return False
    record_key = claimed[0].decode()
    token = claimed[1]
    # A worker that waits for hours between crawls would otherwise reuse a connection
    # Postgres has long since closed.
    close_old_connections()
    fields = {key.decode(): value.decode() for key, value in redis.hgetall(record_key).items()}
    user_id = int(fields["user_id"])
    try:
        _run_round(redis, record_key, token, fields, index_path)
    except Exception:
        # Failed rather than retried: whatever went wrong would most likely go wrong again.
        logger.exception("Seed crawl for user %d failed", user_id)
        _finish(redis, record_key, token, user_id, STATUS_FAILED)
    return True


def _write_if_still_claimed(redis, record_key: str, token: int, write: Callable[[Pipeline], None]) -> bool:
    """Make write's changes in one transaction if the crawl is still this claim's, returning whether it was.

    The token changes whenever the crawl is claimed again or queued afresh, so a worker that
    has been too slow, or whose crawl has since been failed, writes nothing.
    """
    with redis.pipeline() as pipeline:
        try:
            pipeline.watch(record_key)
            if int(pipeline.hget(record_key, "token")) != token:
                return False
            pipeline.multi()
            write(pipeline)
            pipeline.execute()
        except WatchError:
            return False
    return True


def _run_round(redis, record_key: str, token: int, fields: dict[str, str], index_path: str) -> None:
    user_id = int(fields["user_id"])
    domains = json.loads(fields["domains"])

    if "discovered" not in fields:
        # A query's Staan results count towards its domains once per user, however often
        # they crawl it, so nobody can raise a domain's score by repeating a query.
        first_crawl_of_query = bool(redis.hsetnx(record_key, "staan_results_counted", 1))
        discovered = register_seed_domains(user_id, domains, count_staan_results=first_crawl_of_query)
        discovered_json = json.dumps(sorted(discovered))
        if not _write_if_still_claimed(
            redis, record_key, token, lambda pipeline: pipeline.hset(record_key, "discovered", discovered_json)
        ):
            return

    frontier = Frontier.from_json(fields["frontier"])
    crawl_started_at = datetime.fromisoformat(fields["crawl_started_at"])
    elapsed_seconds = (datetime.now(timezone.utc) - crawl_started_at).total_seconds()
    # The per-domain cap already bounds the rounds; this bounds their slow fetches.
    batch = frontier.next_batch() if elapsed_seconds < settings.SEED_CRAWL_MAX_SECONDS else []
    if not batch:
        _finish(redis, record_key, token, user_id, STATUS_DONE, domains)
        return

    if "last_round_started" in fields:
        # One crawl at a time, so this keeps any one site from being fetched from faster.
        next_round_due = float(fields["last_round_started"]) + settings.SEED_CRAWL_DOMAIN_DELAY_SECONDS
        time.sleep(max(0.0, next_round_due - time.time()))
    round_started = time.time()
    documents = crawl_round(frontier, batch, redis)
    # Should the worker die between indexing the round and recording it, the round is run
    # again, and finds its pages already indexed: they are lost from the record, not counted twice.
    indexed = index_new_documents(documents, index_path)
    # Combined Search has since written Staan's snippets of the new seeds, so the index alone
    # no longer says they were new. Only those this write stored count.
    new_seed_urls = set(json.loads(fields["new_seed_urls"]))
    new_urls = indexed.new | (indexed.stored & new_seed_urls)
    candidates = [document for document in documents if document.url in new_urls]

    # A page is counted once per query, however many crawls of it find it new.
    counted_urls_key = _counted_urls_key(record_key)
    pipeline = redis.pipeline()
    for document in candidates:
        pipeline.sismember(counted_urls_key, document.url)
    already_counted = pipeline.execute()
    new_documents = [document for document, counted in zip(candidates, already_counted) if not counted]
    frontier.record_round(batch, {document.url for document in new_documents})
    round_domain_pages = Counter(bare_host(document.url) for document in new_documents)

    def write(pipeline: Pipeline) -> None:
        ttl = settings.SEED_CRAWL_RECORD_TTL_SECONDS
        pipeline.hset(
            record_key,
            mapping={"frontier": frontier.to_json(), "attempts": 0, "last_round_started": round_started},
        )
        pipeline.hincrby(record_key, "pages_crawled", len(batch))
        pipeline.hincrby(record_key, "rounds", 1)
        pipeline.expire(record_key, ttl)
        if new_documents:
            pages = [{"url": doc.url, "title": doc.title, "extract": doc.extract} for doc in new_documents]
            pipeline.sadd(counted_urls_key, *[document.url for document in new_documents])
            pipeline.expire(counted_urls_key, ttl)
            pipeline.rpush(_pages_key(record_key), *[json.dumps(page) for page in pages])
            pipeline.expire(_pages_key(record_key), ttl)
            for domain, count in round_domain_pages.items():
                pipeline.hincrby(_domain_pages_key(record_key), domain, count)
            pipeline.expire(_domain_pages_key(record_key), ttl)
        # Released, and still ahead of every crawl queued after it.
        pipeline.zadd(READY_KEY, {record_key: float(fields["queued_at"])})

    if not _write_if_still_claimed(redis, record_key, token, write):
        return
    logger.info("Seed crawl for user %d added %d new pages", user_id, len(new_documents))
    # After the round is recorded, so a round run twice is not counted twice.
    if new_documents:
        MwmblUser.objects.filter(id=user_id).update(
            seed_search_pages_indexed=F("seed_search_pages_indexed") + len(new_documents)
        )


def _finish(
    redis, record_key: str, token: int, user_id: int, status: str, domains: dict[str, int] | None = None
) -> None:
    """End the crawl with this status, freeing the user for another, then score its domains if it is done."""

    def write(pipeline: Pipeline) -> None:
        pipeline.hset(record_key, mapping={"status": status, "finished_at": _now()})
        pipeline.hdel(record_key, "frontier")
        pipeline.expire(record_key, settings.SEED_CRAWL_RECORD_TTL_SECONDS)
        pipeline.zrem(READY_KEY, record_key)
        pipeline.delete(_active_key(user_id))

    if not _write_if_still_claimed(redis, record_key, token, write) or status != STATUS_DONE:
        return
    # Only a finished crawl is recorded: a failed one's counts would understate its domains.
    domain_pages = {key.decode(): int(value) for key, value in redis.hgetall(_domain_pages_key(record_key)).items()}
    record_seed_domain_crawls({domain: domain_pages.get(domain, 0) for domain in domains})


def get_seed_crawl_summary(user_id: int, query: str) -> dict | None:
    """This user's seed crawl for this query without its pages, or None if there is none.

    Counts the pages rather than reading them, so it stays cheap for a crawl that has added
    thousands.
    """
    redis = get_redis_connection("default")
    record_key = _record_key(user_id, query)
    record = redis.hgetall(record_key)
    if not record:
        return None
    fields = {key.decode(): value.decode() for key, value in record.items()}
    # Every crawl queued or running is in the ready set. Those of crawls queued before they
    # were leased, whose workers died, are not, and are no longer being crawled.
    if fields["status"] in (STATUS_QUEUED, STATUS_CRAWLING) and redis.zscore(READY_KEY, record_key) is None:
        fields["status"] = STATUS_FAILED
    domain_pages = {key.decode(): int(value) for key, value in redis.hgetall(_domain_pages_key(record_key)).items()}
    # Records written before seed domains existed have no domains; they expire within a week.
    domains = list(json.loads(fields.get("domains", "{}")))
    # Absent until the worker has registered the crawl's domains, and with it whether each is
    # new, so there are no domains to report until then.
    if "discovered" not in fields:
        domains = []
    discovered = json.loads(fields.get("discovered", "[]"))
    return {
        "query": fields["query"],
        "status": fields["status"],
        "started_at": fields["started_at"],
        "finished_at": fields.get("finished_at"),
        "pages_crawled": int(fields["pages_crawled"]),
        "pages_indexed": redis.llen(_pages_key(record_key)),
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
    crawl_started_at = fields["crawl_started_at"]
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(crawl_started_at)
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
    pages = [json.loads(page) for page in redis.lrange(_pages_key(_record_key(user_id, query)), 0, -1)]
    return {**summary, "pages": pages}
