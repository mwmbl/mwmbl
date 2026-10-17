"""Promoted domains: the seed domains the server lists for crawlers, best first.

The crawler reads them from GET /api/v2/combined-search/seed-domains and gives them priority
over curated domains: they go into a batch first, queue as deeply as curated ones, and a
promoted domain the crawler has never seen is seeded by its root page.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import fakeredis
import pytest
import requests

from mwmbl import crawl
from mwmbl.crawler.urls import FoundURL, URLStatus
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.redis_url_queue import DOMAIN_SCORE_KEY, MAX_URLS_PER_CORE_DOMAIN, RedisURLQueue
from mwmbl.utils import parse_url

PROMOTED = "promoted.example.com"
CURATED = "curated.example.com"


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True, health_check_interval=30)


def found_url(url: str) -> FoundURL:
    return FoundURL(
        url=url,
        user_id_hash="test_user_hash",
        status=URLStatus.NEW,
        timestamp=datetime.now(timezone.utc),
        last_crawled=None,
    )


def make_queue(fake_redis, promoted: list[str], blacklisted: set[str] = frozenset()) -> RedisURLQueue:
    return RedisURLQueue(
        fake_redis,
        lambda: {CURATED},
        StaticBlacklistProvider(set(blacklisted)),
        get_promoted_domains_function=lambda: promoted,
    )


def test_promoted_domains_come_before_curated_ones(fake_redis):
    queue = make_queue(fake_redis, [PROMOTED])
    queue.queue_urls([found_url(f"https://{CURATED}/page"), found_url(f"https://{PROMOTED}/page")])

    # Room for the seed and one more: the promoted domain must be the one that gets it.
    with patch("mwmbl.redis_url_queue.CRAWL_BATCH_SIZE", 2):
        batch = queue.get_batch("test_user")

    assert batch[1:] == [f"https://{PROMOTED}/page"]


def test_promoted_domains_keep_their_order(fake_redis):
    promoted = [f"promoted{i}.example.com" for i in range(5)]
    queue = make_queue(fake_redis, promoted)
    queue.queue_urls([found_url(f"https://{domain}/page") for domain in reversed(promoted)])

    batch = queue.get_batch("test_user")

    assert [parse_url(url).netloc for url in batch[1:6]] == promoted


def test_an_unqueued_promoted_domain_is_the_seed(fake_redis):
    queue = make_queue(fake_redis, [PROMOTED])

    batch = queue.get_batch("test_user")

    assert batch[0] == f"https://{PROMOTED}/"


def test_the_seed_falls_back_to_curated_domains_once_every_promoted_domain_is_queued(fake_redis):
    queue = make_queue(fake_redis, [PROMOTED])
    queue.queue_urls([found_url(f"https://{PROMOTED}/page")])

    batch = queue.get_batch("test_user")

    assert parse_url(batch[0]).netloc != PROMOTED


def test_a_blacklisted_promoted_domain_is_not_crawled(fake_redis):
    fake_redis.zadd(DOMAIN_SCORE_KEY, {"queued-spam.com": 1.0})
    fake_redis.zadd("domain-urls-queued-spam.com", {"https://queued-spam.com/page": 1.0})
    queue = make_queue(fake_redis, ["spam.com", "queued-spam.com"], blacklisted={"spam.com", "queued-spam.com"})

    batch = queue.get_batch("test_user")

    assert not {parse_url(url).netloc for url in batch} & {"spam.com", "queued-spam.com"}


def test_promoted_domains_queue_as_deeply_as_curated_ones(fake_redis):
    queue = make_queue(fake_redis, [PROMOTED])
    urls = [found_url(f"https://{PROMOTED}/page{i}") for i in range(20)]

    queue.queue_urls(urls)

    assert queue.get_domain_count(PROMOTED) == min(len(urls), MAX_URLS_PER_CORE_DOMAIN)


@pytest.fixture
def fresh_promoted_cache():
    with (
        patch.object(crawl, "_promoted_domains_cache", []),
        patch.object(crawl, "_promoted_domains_fetched_at", None),
        patch.object(crawl, "MWMBL_API_KEY", "crawl-key"),
    ):
        yield


def seed_domains_response(domains: list[dict]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = domains
    return response


def test_the_crawler_fetches_promoted_domains_with_its_key_dropping_exhausted_ones(fresh_promoted_cache):
    response = seed_domains_response(
        [
            {"domain": "best.example.com", "score": 3.0},
            {"domain": "next.example.com", "score": 1.0},
            {"domain": "exhausted.example.com", "score": 0.0},
        ]
    )
    with patch.object(crawl.requests, "get", return_value=response) as get:
        promoted = crawl._fetch_promoted_domains()

    assert promoted == ["best.example.com", "next.example.com"]
    assert get.call_args.args[0].endswith("/api/v2/combined-search/seed-domains")
    assert get.call_args.kwargs["headers"]["X-API-Key"] == "crawl-key"


def test_a_failed_fetch_is_not_retried_until_the_cache_expires(fresh_promoted_cache):
    response = seed_domains_response([])
    response.raise_for_status.side_effect = requests.HTTPError("401")
    with patch.object(crawl.requests, "get", return_value=response) as get:
        assert crawl._fetch_promoted_domains() == []
        assert crawl._fetch_promoted_domains() == []

    assert get.call_count == 1


def test_a_crawler_without_a_key_has_no_promoted_domains(fresh_promoted_cache):
    with patch.object(crawl, "MWMBL_API_KEY", ""), patch.object(crawl.requests, "get") as get:
        assert crawl._fetch_promoted_domains() == []

    get.assert_not_called()
