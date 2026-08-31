"""
Regression tests for URL de-duplication.

A freshly-crawled URL (or one that failed, e.g. robots.txt denied) must be
recorded as crawled *now* so that it is not immediately re-queued and crawled
again. The bug these tests guard against was that `update_found_urls` derived
`last_crawled` only from the bloom-filter state *before* the current batch, so a
first-time crawl came back with `last_crawled=None` and `queue_urls` treated it
as never-crawled.
"""
import glob
import os
from datetime import datetime

import fakeredis
import pytest

from mwmbl.crawler.urls import URLDatabase, FoundURL, URLStatus
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.redis_url_queue import RedisURLQueue


@pytest.fixture
def clean_bloom_filters():
    """Remove any bloom filter files so each test starts from a clean slate."""
    def _remove():
        for path in glob.glob("/tmp/test_urls*.bloom"):
            os.remove(path)

    _remove()
    yield
    _remove()


def test_crawled_url_marked_as_recently_crawled(clean_bloom_filters):
    """A first-time crawled URL should come back with last_crawled set to the crawl time."""
    crawled_at = datetime.utcnow()
    found = FoundURL(
        url="https://example.com/page",
        user_id_hash="user",
        status=URLStatus.CRAWLED,
        timestamp=crawled_at,
        last_crawled=None,
    )
    with URLDatabase() as url_db:
        new_urls = url_db.update_found_urls([found])

    assert len(new_urls) == 1
    assert new_urls[0].last_crawled == crawled_at


def test_robots_denied_url_marked_as_recently_crawled(clean_bloom_filters):
    """A robots-denied URL must also be marked crawled so it isn't re-attempted."""
    crawled_at = datetime.utcnow()
    found = FoundURL(
        url="https://example.com/blocked",
        user_id_hash="user",
        status=URLStatus.ERROR_ROBOTS_DENIED,
        timestamp=crawled_at,
        last_crawled=None,
    )
    with URLDatabase() as url_db:
        new_urls = url_db.update_found_urls([found])

    assert new_urls[0].last_crawled == crawled_at


def test_crawled_url_is_not_requeued(clean_bloom_filters):
    """End-to-end: a just-crawled URL is not put back on the queue, but a newly
    discovered link is."""
    crawled = FoundURL(
        url="https://crawled.example/page",
        user_id_hash="user",
        status=URLStatus.CRAWLED,
        timestamp=datetime.utcnow(),
        last_crawled=None,
    )
    new_link = FoundURL(
        url="https://discovered.example/page",
        user_id_hash="user",
        status=URLStatus.NEW,
        timestamp=datetime.utcnow(),
        last_crawled=None,
    )

    with URLDatabase() as url_db:
        new_urls = url_db.update_found_urls([crawled, new_link])

    redis = fakeredis.FakeRedis(decode_responses=True)
    url_queue = RedisURLQueue(redis, lambda: set(), StaticBlacklistProvider(set()))
    url_queue.queue_urls(new_urls)

    assert url_queue.get_domain_count("crawled.example") == 0
    assert url_queue.get_domain_count("discovered.example") == 1


def test_crawl_date_is_the_crawl_time_not_the_start_of_the_month(clean_bloom_filters):
    """The bloom filters only record the month a URL was crawled in. Reporting the start
    of that month as the crawl date makes a URL crawled on the 31st look 30 days old -
    exactly the age at which queue_urls hands it out again."""
    crawled_at = datetime(2026, 8, 31, 12, 0, 0)
    found = FoundURL(
        url="https://end-of-month.example/page",
        user_id_hash="user",
        status=URLStatus.CRAWLED,
        timestamp=crawled_at,
        last_crawled=None,
    )
    with URLDatabase() as url_db:
        new_urls = url_db.update_found_urls([found])

    assert new_urls[0].last_crawled == crawled_at


def test_url_queue_gives_its_curated_domains_to_the_default_blacklist():
    """The standalone crawler has no database and fetches approved domains over HTTP, so
    the queue's own source of curated domains is what has to reach the blacklist."""
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    url_queue = RedisURLQueue(redis, lambda: {"pudding.cool"})

    assert url_queue.blacklist_provider.get_exempt_domains() == {"pudding.cool"}
