"""Which crawled results the crawler contributes to the main index.

Contributing to a term the main index already covers means displacing something: pages hold
a fixed number of bytes and the server keeps the best-ranked documents that fit. So the gate
asks how many of our new items would survive that, and submits the term only when enough of
them would - see count_new_index_entries.

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

from mwmbl.crawl import Crawler, count_new_index_entries
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


def test_an_item_that_outranks_the_incumbent_is_a_new_entry():
    assert count_new_index_entries(TERM, [STRONG], [MEDIUM]) == 1


def test_an_item_the_incumbent_outranks_is_not():
    assert count_new_index_entries(TERM, [MEDIUM], [STRONG]) == 0


def test_ties_go_to_the_incumbent():
    """Nothing to gain from resubmitting what the main index already has."""
    assert count_new_index_entries(TERM, [MEDIUM], [MEDIUM]) == 0


def test_every_matching_item_is_a_new_entry_for_a_term_the_index_has_never_seen():
    """The perverse case under the old comparison: fresh results for an unknown term.

    There is nothing to displace, so anything that matches the term at all is a new entry.
    """
    assert count_new_index_entries(TERM, [MEDIUM, WEAKER_MEDIUM], []) == 2


def test_items_that_match_nothing_are_never_new_entries():
    """A 0.0 score is not a contribution, whether or not the index has the term."""
    assert count_new_index_entries(TERM, [WEAK], []) == 0
    assert count_new_index_entries(TERM, [WEAK], [MEDIUM]) == 0


def test_a_synced_mirror_document_that_matches_nothing_is_not_a_new_entry():
    """SYNCED_WITH_MAIN_INDEX is bookkeeping, not curation.

    score_result exempts curated documents from its majority-terms rule. This local index
    is full of documents marked SYNCED_WITH_MAIN_INDEX by run_indexing, and they used to
    ride that exemption: a mirrored document scored above 0.0 for a term it did not match,
    which is exactly the score that clears the bar for an unseen term.
    """
    mirrored = Document(
        title=WEAK.title, url=WEAK.url, extract=WEAK.extract, state=DocumentState.SYNCED_WITH_MAIN_INDEX
    )
    assert count_new_index_entries(TERM, [mirrored], []) == 0


def test_a_curated_document_keeps_its_exemption():
    """The exemption is still there for the documents it was written for."""
    curated = Document(title=WEAK.title, url=WEAK.url, extract=WEAK.extract, state=DocumentState.FROM_USER)
    assert score_result(tokenize(TERM), curated, True) > 0.0


def test_nothing_new_is_nothing_to_contribute():
    assert count_new_index_entries(TERM, [], [MEDIUM]) == 0


def test_one_weak_remote_result_leaves_room_for_a_middling_one():
    """The bar is displacement, not beating the best result there is.

    Comparing against the best remote result made contribution all-or-nothing: any term the
    main index already covered well was closed to us, however much we had to add. A result
    that outranks the weakest of two incumbents takes its slot.
    """
    assert count_new_index_entries(TERM, [MEDIUM], [WEAK, STRONG]) == 1


def test_the_estimate_is_capped_by_the_slots_there_are():
    """Three good items cannot all land in a term the index holds two documents for."""
    assert count_new_index_entries(TERM, [STRONG, MEDIUM, WEAKER_MEDIUM], [WEAK, WEAK]) == 2


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
