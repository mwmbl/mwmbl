"""How often one crawler fetches from any one domain, and what it does with a refusal."""

from datetime import datetime, timezone

import fakeredis
import pytest

from mwmbl.crawler import politeness, retrieve
from mwmbl.crawler.batch import HashedBatch, Item
from mwmbl.crawler.politeness import (
    BLOCK_COOL_OFF_SECONDS,
    DEFAULT_DOMAIN_DELAY_SECONDS,
    DOMAIN_CLAIM_KEY,
    MAX_BLOCK_COOL_OFF_SECONDS,
    claim_domain,
    release_domains,
)
from mwmbl.crawler.urls import FoundURL, URLStatus
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.index_batches import get_documents_from_batches
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.utils import parse_url

URL = "https://example.test/page"
CLAIM = DOMAIN_CLAIM_KEY.format(domain="example.test")


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def robots(monkeypatch):
    """Serve robots.txt from a variable the test can set."""
    served = {"content": b""}
    monkeypatch.setattr(retrieve, "fetch", lambda url: (200, served["content"], url))
    return served


def _result(status: int | None, url: str = URL) -> dict:
    return {"url": url, "status": status}


def test_a_claimed_domain_cannot_be_claimed_again(redis):
    assert claim_domain(redis, "example.test")
    assert not claim_domain(redis, "example.test")
    assert claim_domain(redis, "other.test")


def test_release_holds_the_domain_for_its_crawl_delay(redis, robots):
    robots["content"] = b"User-agent: *\nCrawl-delay: 30\n"

    release_domains(redis, [_result(200)])

    assert 25 <= redis.ttl(CLAIM) <= 30
    assert not claim_domain(redis, "example.test")


def test_release_holds_the_domain_for_at_least_the_default_delay(redis, robots):
    robots["content"] = b"User-agent: *\nCrawl-delay: 1\n"

    release_domains(redis, [_result(200)])

    assert DEFAULT_DOMAIN_DELAY_SECONDS - 2 <= redis.ttl(CLAIM) <= DEFAULT_DOMAIN_DELAY_SECONDS


def test_unavailable_robots_still_gets_the_default_delay(redis, monkeypatch):
    def unreachable(url):
        raise ConnectionError("refused")

    monkeypatch.setattr(retrieve, "fetch", unreachable)

    release_domains(redis, [_result(None)])

    assert DEFAULT_DOMAIN_DELAY_SECONDS - 2 <= redis.ttl(CLAIM) <= DEFAULT_DOMAIN_DELAY_SECONDS


@pytest.mark.parametrize("status", [403, 429, 503])
def test_each_refusal_doubles_the_cool_off_until_a_fetch_gets_through(redis, robots, status):
    release_domains(redis, [_result(status)])
    assert redis.ttl(CLAIM) == BLOCK_COOL_OFF_SECONDS

    release_domains(redis, [_result(status)])
    assert redis.ttl(CLAIM) == 2 * BLOCK_COOL_OFF_SECONDS

    release_domains(redis, [_result(200)])
    release_domains(redis, [_result(status)])
    assert redis.ttl(CLAIM) == BLOCK_COOL_OFF_SECONDS


def test_the_cool_off_is_capped(redis, robots):
    redis.set(politeness.DOMAIN_BLOCKS_KEY.format(domain="example.test"), 50)

    release_domains(redis, [_result(429)])

    assert redis.ttl(CLAIM) == MAX_BLOCK_COOL_OFF_SECONDS


def _queue(redis) -> RedisURLQueue:
    queue = RedisURLQueue(redis, lambda: set(), StaticBlacklistProvider(set()))
    queue.queue_urls(
        [
            FoundURL(url, "user", URLStatus.NEW, datetime.now(timezone.utc), None)
            for url in ["https://example.test/page", "https://other.test/page"]
        ]
    )
    return queue


def test_a_batch_leaves_a_held_domain_and_its_urls_for_later(redis):
    queue = _queue(redis)
    claim_domain(redis, "example.test")

    batch = queue.get_batch("user")

    assert "https://example.test/page" not in batch
    assert "https://other.test/page" in batch
    assert queue.get_domain_count("example.test") == 1


def test_a_second_batch_does_not_take_the_domains_of_the_first(redis):
    queue = _queue(redis)

    first = queue.get_batch("user")
    queue.queue_urls([FoundURL("https://other.test/next", "user", URLStatus.NEW, datetime.now(timezone.utc), None)])
    second = queue.get_batch("user")

    first_domains = {parse_url(url).netloc for url in first}
    second_domains = {parse_url(url).netloc for url in second}
    assert "other.test" in first_domains
    assert not first_domains & second_domains
    assert queue.get_domain_count("other.test") == 1


@pytest.mark.parametrize(("status", "indexed"), [(200, True), (None, True), (403, False), (503, False)])
def test_only_pages_a_server_actually_served_are_indexed(status, indexed):
    item = {
        "url": URL,
        "status": status,
        "timestamp": 0,
        "content": {"title": "Just a moment...", "extract": "Checking your browser", "links": [URL + "/next"]},
        "error": None,
    }
    batch = HashedBatch.parse_obj({"user_id_hash": "user", "timestamp": 0, "items": [item]})

    assert (Item.parse_obj(item).page is not None) is indexed
    assert bool(list(get_documents_from_batches([batch]))) is indexed
