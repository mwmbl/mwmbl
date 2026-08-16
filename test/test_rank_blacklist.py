"""Retrieval-time blacklist filtering: the guarantee that a blacklisted domain is never
shown, plus the enqueueing that lets the background purge clean the index up afterwards."""
from unittest.mock import patch

import fakeredis
import pytest

from mwmbl.indexer import purge_queue
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist
from mwmbl.indexer.purge_queue import PURGE_QUEUE_KEY, drain_purge_queue
from mwmbl.tinysearchengine.indexer import Document, DocumentState
from mwmbl.tinysearchengine.rank import HeuristicRanker

BLACKLISTED_DOMAIN = "badsite.test"

BAD = Document(title="Bananas", url=f"https://{BLACKLISTED_DOMAIN}/bananas",
               extract="bananas and apples", score=1.0)
GOOD = Document(title="Bananas", url="https://example.test/bananas",
                extract="bananas and apples", score=1.0)


class FakeIndex:
    """Returns the same documents for every term, like a page every query term hits."""

    def __init__(self, documents):
        self.documents = documents

    def retrieve(self, key):
        return list(self.documents)


class FakeCompleter:
    def complete(self, term):
        return []


@pytest.fixture(autouse=True)
def clear_recently_enqueued():
    """purge_queue keeps a process-level record of what it has already queued, so without
    this each test would inherit the previous one's suppressions."""
    purge_queue._recently_enqueued.clear()
    yield
    purge_queue._recently_enqueued.clear()


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def blacklist(redis_client):
    """A snapshot-free blacklist: the built-in rules alone are enough to decide here, so
    these tests exercise the ranker rather than the snapshot machinery."""
    return SnapshotBlacklist(built_in_rules=StaticBlacklistProvider({BLACKLISTED_DOMAIN}),
                             redis_client=redis_client)


@pytest.fixture
def ranker(blacklist, redis_client, request):
    documents = getattr(request, "param", [BAD, GOOD])
    with patch("mwmbl.tinysearchengine.rank.get_snapshot_blacklist", return_value=blacklist), \
            patch.object(purge_queue, "get_redis", return_value=redis_client):
        yield HeuristicRanker(FakeIndex(documents), FakeCompleter())


def test_search_drops_blacklisted_results(ranker):
    urls = [result.url for result in ranker.search("bananas", [])]

    assert BAD.url not in urls
    assert GOOD.url in urls


def test_search_queues_blacklisted_results_for_purging(ranker, redis_client):
    ranker.search("bananas", [])

    queued = drain_purge_queue(10, redis_client)
    assert [d.url for d in queued] == [BAD.url]
    # The queued payload carries what tokenize_document needs to find the document's pages
    assert queued[0].title == BAD.title
    assert queued[0].extract == BAD.extract


def test_a_clean_result_set_queues_nothing(ranker, redis_client):
    with patch.object(ranker, "tiny_index", FakeIndex([GOOD])):
        assert [r.url for r in ranker.search("bananas", [])] == [GOOD.url]

    assert redis_client.scard(PURGE_QUEUE_KEY) == 0


def test_the_same_document_is_queued_once_across_queries(ranker, redis_client):
    """A blacklisted document stays in the index until the purge runs, so it keeps
    turning up. It must neither pile up in the queue nor cost a Redis round trip on
    every query in the meantime."""
    for _ in range(5):
        ranker.search("bananas", [])

    assert redis_client.scard(PURGE_QUEUE_KEY) == 1
    assert len(purge_queue._recently_enqueued) == 1


def test_a_document_is_re_queued_once_the_suppression_expires(ranker, redis_client):
    """If a purge is lost the document has to get another chance, or it stays in the
    index for as long as this process lives."""
    ranker.search("bananas", [])
    assert drain_purge_queue(10, redis_client)

    purge_queue._recently_enqueued.clear()  # stands in for the TTL expiring
    ranker.search("bananas", [])

    assert [d.url for d in drain_purge_queue(10, redis_client)] == [BAD.url]


def test_get_raw_results_drops_blacklisted_results(ranker):
    """/raw bypasses get_results(), so it needs filtering of its own."""
    urls = [result.url for result in ranker.get_raw_results("bananas")]

    assert BAD.url not in urls
    assert GOOD.url in urls


def test_complete_does_not_suggest_blacklisted_urls(ranker):
    _, completions = ranker.complete("banana")

    assert not any(BLACKLISTED_DOMAIN in completion for completion in completions)


def test_curated_blacklisted_documents_are_dropped(blacklist, redis_client):
    """Curated items bypass order_results entirely - they are prepended straight into
    deduplicate() - so they need filtering separately from the ranked candidates."""
    curated = Document(title="Bananas", url=f"https://{BLACKLISTED_DOMAIN}/curated",
                       extract="bananas", score=1.0, term="bananas",
                       state=DocumentState.ORGANIC_APPROVED)

    with patch("mwmbl.tinysearchengine.rank.get_snapshot_blacklist", return_value=blacklist), \
            patch.object(purge_queue, "get_redis", return_value=redis_client):
        ranker = HeuristicRanker(FakeIndex([curated, GOOD]), FakeCompleter())
        urls = [result.url for result in ranker.search("bananas", [])]

    assert curated.url not in urls
    assert GOOD.url in urls


def test_additional_results_are_filtered_but_not_queued(blacklist, redis_client):
    """Caller-supplied results (the Google-sourced documents the search page passes in)
    must be filtered too, but there is nothing in our index to purge for them."""
    additional = Document(title="Bananas", url=f"https://{BLACKLISTED_DOMAIN}/from-google",
                          extract="bananas", score=1.0, state=DocumentState.FROM_GOOGLE)

    with patch("mwmbl.tinysearchengine.rank.get_snapshot_blacklist", return_value=blacklist), \
            patch.object(purge_queue, "get_redis", return_value=redis_client):
        ranker = HeuristicRanker(FakeIndex([GOOD]), FakeCompleter())
        urls = [result.url for result in ranker.search("bananas", [additional])]

    assert additional.url not in urls
    assert redis_client.scard(PURGE_QUEUE_KEY) == 0


def test_filtering_can_be_switched_off(ranker, settings):
    """The kill switch has to work without a rollback if filtering misbehaves in prod."""
    settings.BLACKLIST_FILTER_AT_RETRIEVAL = False

    assert BAD.url in [result.url for result in ranker.search("bananas", [])]


def test_a_redis_failure_does_not_break_search(blacklist):
    """Enqueueing is best-effort: losing an entry just means the next query re-queues it,
    but a search response must never fail because of it."""
    class BrokenRedis:
        def scard(self, key):
            raise ConnectionError("redis is down")

    with patch("mwmbl.tinysearchengine.rank.get_snapshot_blacklist", return_value=blacklist), \
            patch.object(purge_queue, "get_redis", return_value=BrokenRedis()):
        ranker = HeuristicRanker(FakeIndex([BAD, GOOD]), FakeCompleter())
        urls = [result.url for result in ranker.search("bananas", [])]

    assert BAD.url not in urls
    assert GOOD.url in urls
