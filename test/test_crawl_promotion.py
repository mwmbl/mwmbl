"""Which crawled results the crawler contributes to the main index.

Contributing to a term the main index already covers means displacing something: pages hold
a fixed number of bytes and the server keeps the best-ranked documents that fit.

The comparison used to run the other way round entirely (`max_local_score < min_remote_score`),
which promoted results precisely when they were the weaker ones and, because scores are never
negative, promoted nothing at all whenever any remote item scored 0.0 or the term was missing
from the remote index. These cover both directions so it stays the right way round.
"""

import time
from collections import Counter
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from mwmbl.crawl import Crawler
from mwmbl.crawler.batch import HashedBatch, Item, ItemContent
from mwmbl.tinysearchengine.indexer import Document, DocumentState
from mwmbl.tinysearchengine.rank import score_result
from mwmbl.tokenizer import tokenize

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
WEAKER_MEDIUM = Document(
    title="Some notes on python",
    url="https://example.net/blog/archive/notes/python",
    extract="A few things I learned about python",
)
WEAK = Document(
    title="Cooking recipes",
    url="https://example.com/recipes/some/long/path",
    extract="Nothing to do with snakes",
)


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True, health_check_interval=30)


def test_the_fixtures_are_ordered_as_the_tests_assume():
    """Every expectation below rests on this ordering, so state it once and check it."""
    terms = tokenize(TERM)
    scores = [score_result(terms, doc, True) for doc in [STRONG, MEDIUM, WEAKER_MEDIUM, WEAK]]
    assert scores[0] > scores[1] > scores[2] > 0.0
    assert scores[3] == 0.0


def test_a_curated_document_keeps_its_exemption():
    """The exemption is still there for the documents it was written for."""
    curated = Document(title=WEAK.title, url=WEAK.url, extract=WEAK.extract, state=DocumentState.FROM_USER)
    assert score_result(tokenize(TERM), curated, True) > 0.0


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

        post.return_value.json = lambda: {"status": "ok", "url": None, "new_items": []}

        crawler.run_indexing()

    return post


def test_run_indexing_submits_results_that_beat_the_remote_index(fake_redis):
    post = run_indexing_with(fake_redis, [STRONG], [MEDIUM])

    post.assert_called_once()
    submitted_urls = [result["url"] for result in post.call_args.kwargs["json"]["results"]]
    assert submitted_urls == [STRONG.url]


def test_run_indexing_sends_the_api_key_in_the_header(fake_redis):
    post = run_indexing_with(fake_redis, [STRONG], [MEDIUM])

    headers = post.call_args.kwargs["headers"]
    assert headers["X-API-Key"] == "test-api-key"
    # Our own API gets the crawler's API identity, not the contact address meant for
    # third-party sites.
    assert headers["User-Agent"].startswith("mwmbl-crawler/")
    assert post.call_args.kwargs["json"]["api_key"] is None


def test_run_indexing_submits_the_whole_term_once_the_gate_opens(fake_redis):
    """Deliberate: the gate is per term, the payload is every new item for it.

    A weak item cannot jump the queue - the server ranks it against the term's existing
    documents on the way in - and it may be the right answer for a term we never asked
    about. Submitting one already in the index also refreshes its stored crawl date.
    """
    post = run_indexing_with(fake_redis, [STRONG, WEAK], [MEDIUM])

    post.assert_called_once()
    submitted_urls = {result["url"] for result in post.call_args.kwargs["json"]["results"]}
    assert submitted_urls == {STRONG.url, WEAK.url}
