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


def _current_month():
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)


def test_crawled_url_marked_as_recently_crawled(clean_bloom_filters):
    """A first-time crawled URL should come back with last_crawled set to now."""
    found = FoundURL(
        url="https://example.com/page",
        user_id_hash="user",
        status=URLStatus.CRAWLED,
        timestamp=datetime.utcnow(),
        last_crawled=None,
    )
    with URLDatabase() as url_db:
        new_urls = url_db.update_found_urls([found])

    assert len(new_urls) == 1
    assert new_urls[0].last_crawled == _current_month()


def test_robots_denied_url_marked_as_recently_crawled(clean_bloom_filters):
    """A robots-denied URL must also be marked crawled so it isn't re-attempted."""
    found = FoundURL(
        url="https://example.com/blocked",
        user_id_hash="user",
        status=URLStatus.ERROR_ROBOTS_DENIED,
        timestamp=datetime.utcnow(),
        last_crawled=None,
    )
    with URLDatabase() as url_db:
        new_urls = url_db.update_found_urls([found])

    assert new_urls[0].last_crawled == _current_month()


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
