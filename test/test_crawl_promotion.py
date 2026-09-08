"""Which crawled results the crawler contributes to the main index.

The gate compares the best new local result against the best result the remote index
already holds for a term, and higher scores are better. It used to compare the other way
round - `max_local_score < min_remote_score` - which promoted results precisely when they
were the weaker ones, and, because scores are never negative, promoted nothing at all
whenever any remote item scored 0.0 or the term was missing from the remote index
entirely. These cover both directions so it stays the right way round.
"""

import time
from collections import Counter
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from mwmbl.crawl import Crawler, is_new_high_score
from mwmbl.crawler.batch import HashedBatch, Item, ItemContent
from mwmbl.tinysearchengine.indexer import Document

TERM = "python"

# Real documents scored by the real scorer: strong > medium > weak, and weak scores exactly
# 0.0 because it matches none of the term.
STRONG = Document(
    title="Python",
    url="https://python.org",
    extract="The official home of the Python programming language",
)
MEDIUM = Document(
    title="Python tutorial",
    url="https://example.com/python/tutorial",
    extract="Learn python here",
)
WEAK = Document(
    title="Cooking recipes",
    url="https://example.com/recipes/some/long/path",
    extract="Nothing to do with snakes",
)


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True, health_check_interval=30)


def test_better_local_results_are_promoted():
    assert is_new_high_score(TERM, [STRONG], [MEDIUM])


def test_worse_local_results_are_not_promoted():
    assert not is_new_high_score(TERM, [MEDIUM], [STRONG])


def test_equally_good_local_results_are_not_promoted():
    """Nothing to gain from resubmitting what the main index already has."""
    assert not is_new_high_score(TERM, [MEDIUM], [MEDIUM])


def test_a_term_the_remote_index_has_never_seen_is_promoted():
    """The perverse case under the old comparison: fresh results for an unknown term.

    An empty remote result set scores 0.0, so any local item that matches the term at all
    wins. That is the intent - a result beats no result.
    """
    assert is_new_high_score(TERM, [MEDIUM], [])


def test_local_results_that_match_nothing_are_not_promoted():
    """A 0.0 local score must not clear the empty-remote bar."""
    assert not is_new_high_score(TERM, [WEAK], [])


def test_no_new_local_items_is_not_a_high_score():
    assert not is_new_high_score(TERM, [], [MEDIUM])


def test_one_weak_remote_result_does_not_open_the_gate():
    """The bar is the best remote result, not the worst.

    Weak matches score 0.0 and are common, so comparing against the minimum would promote
    anything at all as soon as one poor result was indexed for the term.
    """
    assert not is_new_high_score(TERM, [MEDIUM], [WEAK, STRONG])


def run_indexing_with(redis, local_items: list[Document], remote_items: list[Document]) -> MagicMock:
    """Index one queued batch against the given local and remote index contents.

    Returns the patched requests.post, which is how results reach the main index.
    """
    item = Item(
        url="https://python.org",
        status=200,
        timestamp=time.time(),
        content=ItemContent(title="Python", extract="The Python programming language"),
    )
    batch = HashedBatch(user_id_hash="test_user", timestamp=int(time.time() * 1000), items=[item])
    redis.rpush("batch-queue", batch.json())

    crawler = Crawler()
    crawler._redis = redis

    with (
        patch("mwmbl.crawl.index_batches", return_value=Counter({TERM: 1})),
        patch("mwmbl.crawl.index_pages"),
        patch("mwmbl.crawl.RemoteIndex") as remote_index,
        patch("mwmbl.crawl.TinyIndex") as tiny_index,
        patch("mwmbl.crawl.CRAWL_SUBMIT_MODE", "index"),
        patch("mwmbl.crawl.MWMBL_API_KEY", "test-api-key"),
        patch("requests.post") as post,
    ):
        remote_index.return_value.retrieve.return_value = remote_items

        local_index = tiny_index.return_value.__enter__.return_value
        local_index.retrieve.return_value = local_items
        local_index.get_key_page_index.return_value = 0

        post.return_value.text = "OK"

        crawler.run_indexing()

    return post


def test_run_indexing_submits_results_that_beat_the_remote_index(fake_redis):
    post = run_indexing_with(fake_redis, [STRONG], [MEDIUM])

    post.assert_called_once()
    submitted_urls = [result["url"] for result in post.call_args.kwargs["json"]["results"]]
    assert submitted_urls == [STRONG.url]


def test_run_indexing_keeps_quiet_about_results_the_remote_index_beats(fake_redis):
    post = run_indexing_with(fake_redis, [MEDIUM], [STRONG])

    post.assert_not_called()
