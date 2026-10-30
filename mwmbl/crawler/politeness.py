"""
How often one crawler fetches from any one domain.

A batch holds at most one URL per domain, but that alone only spaces fetches within a batch:
the core domains are in every batch, so with a few workers each one used to be fetched every
few seconds, around the clock, from a volunteer's home connection. That is how a crawler gets
its owner's IP address rate limited or banned - Hacker News asks for Crawl-delay: 30.

So a batch claims each domain it takes a URL from, and no other batch on the same Redis can
take one from that domain until the claim is released. Releasing it after the batch leaves
the domain unavailable for its delay: the Crawl-delay robots.txt asks for, at least
DEFAULT_DOMAIN_DELAY_SECONDS, or a growing cool-off when the site turned us away.
"""

import math
from logging import getLogger

from redis import Redis

from mwmbl.crawler.env_vars import CRAWL_BATCH_SIZE, CRAWL_DELAY_SECONDS, CRAWL_THREADS
from mwmbl.crawler.retrieve import (
    MAX_REDIRECTS,
    ROBOTS_CACHE_ERROR_TTL_SECONDS,
    TIMEOUT_SECONDS,
    RobotsUnavailable,
    robots_crawl_delay,
)
from mwmbl.utils import parse_url

logger = getLogger(__name__)

DOMAIN_CLAIM_KEY = "domain-claim-{domain}"
DOMAIN_BLOCKS_KEY = "domain-blocks-{domain}"

DEFAULT_DOMAIN_DELAY_SECONDS = 5

# A 403 is often a site's answer to every request it thinks is a bot - Cloudflare's challenge
# page is a 403 - so it is treated like a rate limit rather than as one forbidden page.
BLOCKED_STATUSES = {403, 429, 502, 503, 504}
BLOCK_COOL_OFF_SECONDS = 60
MAX_BLOCK_COOL_OFF_SECONDS = 6 * 60 * 60
# Each block doubles the cool-off until a fetch gets through, or the domain goes this long
# without one.
BLOCK_MEMORY_SECONDS = 24 * 60 * 60
# Crawl-delay is whatever digits a site writes, and Redis refuses an expiry past 64 bits.
MAX_DOMAIN_DELAY_SECONDS = MAX_BLOCK_COOL_OFF_SECONDS


def _claim_seconds() -> int:
    """Long enough to outlast the slowest batch these settings allow.

    A claim that expired mid-batch would let a second batch onto a domain the first is still
    crawling. The slowest URL is a robots.txt and a page, each through every redirect, with a
    connect and a read timeout per hop, then the thread's delay. Only a worker that dies
    mid-batch leaves a claim to run this long.
    """
    slowest_fetch_seconds = 2 * (MAX_REDIRECTS + 1) * 2 * TIMEOUT_SECONDS
    urls_per_thread = math.ceil(CRAWL_BATCH_SIZE / CRAWL_THREADS)
    # The floor covers the pool handing a thread a chunk more than its share.
    return max(10 * 60, math.ceil((urls_per_thread + 2) * (slowest_fetch_seconds + 1.1 * CRAWL_DELAY_SECONDS)))


DOMAIN_CLAIM_SECONDS = _claim_seconds()


def claim_domain(redis: Redis, domain: str) -> bool:
    """Take the domain for this batch, if no other batch has it and its delay has passed."""
    return bool(redis.set(DOMAIN_CLAIM_KEY.format(domain=domain), 1, nx=True, ex=DOMAIN_CLAIM_SECONDS))


def release_domains(redis: Redis, results: list[dict]):
    """Make each crawled domain available again once its delay has passed."""
    for result in results:
        domain = parse_url(result["url"]).netloc
        delay = _domain_delay(redis, domain, result)
        redis.set(DOMAIN_CLAIM_KEY.format(domain=domain), 1, ex=delay)

        # A redirect fetched from another domain - www.example.com for example.com, or a
        # shortener's target - so that domain waits too, unless a batch already holds it.
        # Only results that reached a server have one.
        resolved_url = result.get("resolved_url")
        resolved_domain = parse_url(resolved_url).netloc if resolved_url else domain
        if resolved_domain != domain:
            redis.set(DOMAIN_CLAIM_KEY.format(domain=resolved_domain), 1, ex=delay, nx=True)


def abandon_domains(redis: Redis, urls: list[str]):
    """Release a failed batch's domains, rather than leave them held for DOMAIN_CLAIM_SECONDS."""
    for url in urls:
        redis.set(DOMAIN_CLAIM_KEY.format(domain=parse_url(url).netloc), 1, ex=DEFAULT_DOMAIN_DELAY_SECONDS)


def _domain_delay(redis: Redis, domain: str, result: dict) -> int:
    blocks_key = DOMAIN_BLOCKS_KEY.format(domain=domain)
    if result["status"] in BLOCKED_STATUSES:
        blocks, _ = redis.pipeline().incr(blocks_key).expire(blocks_key, BLOCK_MEMORY_SECONDS).execute()
        cool_off = min(BLOCK_COOL_OFF_SECONDS * 2 ** (blocks - 1), MAX_BLOCK_COOL_OFF_SECONDS)
        logger.info(f"HTTP {result['status']} from {domain}, leaving it alone for {cool_off}s")
        return cool_off

    if result["status"] is not None:
        redis.delete(blocks_key)

    try:
        crawl_delay = robots_crawl_delay(result["url"], redis)
    except RobotsUnavailable:
        # Every URL handed out before robots.txt is looked at again would come straight back
        # as RobotsUnavailable, unfetched, and be marked as an error - draining the queue.
        return ROBOTS_CACHE_ERROR_TTL_SECONDS
    return min(max(crawl_delay or 0, DEFAULT_DOMAIN_DELAY_SECONDS), MAX_DOMAIN_DELAY_SECONDS)
