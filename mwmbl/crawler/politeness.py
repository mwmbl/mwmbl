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

from logging import getLogger

from redis import Redis

from mwmbl.crawler.retrieve import RobotsUnavailable, robots_crawl_delay
from mwmbl.utils import parse_url

logger = getLogger(__name__)

DOMAIN_CLAIM_KEY = "domain-claim-{domain}"
DOMAIN_BLOCKS_KEY = "domain-blocks-{domain}"

# Long enough to outlast any batch, so a domain is never handed out twice at once. Only a
# worker that dies mid-batch leaves a claim to run this long.
DOMAIN_CLAIM_SECONDS = 10 * 60
DEFAULT_DOMAIN_DELAY_SECONDS = 5

# A 403 is often a site's answer to every request it thinks is a bot - Cloudflare's challenge
# page is a 403 - so it is treated like a rate limit rather than as one forbidden page.
BLOCKED_STATUSES = {403, 429, 502, 503, 504}
BLOCK_COOL_OFF_SECONDS = 60
MAX_BLOCK_COOL_OFF_SECONDS = 6 * 60 * 60
# Each block doubles the cool-off until a fetch gets through, or the domain goes this long
# without one.
BLOCK_MEMORY_SECONDS = 24 * 60 * 60


def claim_domain(redis: Redis, domain: str) -> bool:
    """Take the domain for this batch, if no other batch has it and its delay has passed."""
    return bool(redis.set(DOMAIN_CLAIM_KEY.format(domain=domain), 1, nx=True, ex=DOMAIN_CLAIM_SECONDS))


def release_domains(redis: Redis, results: list[dict]):
    """Make each crawled domain available again once its delay has passed."""
    for result in results:
        domain = parse_url(result["url"]).netloc
        delay = _domain_delay(redis, domain, result)
        redis.set(DOMAIN_CLAIM_KEY.format(domain=domain), 1, ex=delay)


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
        crawl_delay = None
    return max(crawl_delay or 0, DEFAULT_DOMAIN_DELAY_SECONDS)
