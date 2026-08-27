"""Tests for the dedicated Wikipedia cache index (mwmbl.indexer.wiki_cache).

Where these overlap with the tests on the #357 branch they are deliberately the same
assertions - the term derivation and the freshness rule are carried over unchanged. What is
gone is everything that existed only because cache entries shared a file with real index
content: the anonymisation gate, the Redis queue, the general-indexing path, and the guards
stopping a cache term reaching curation or /raw.
"""
import random
import string
import time
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from mwmbl.apps import create_index
from mwmbl.indexer.wiki_cache import (
    WIKI_CACHE_EMPTY_URL,
    WIKI_CACHE_TERM_PREFIX,
    get_cached_wiki_results,
    is_fresh,
    store_wiki_results,
    wiki_cache_path,
    wiki_cache_term,
)
from mwmbl.tinysearchengine import rank
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, DocumentState, TinyIndex
from mwmbl.tinysearchengine.rank import Ranker, get_wiki_results

NUM_PAGES = 8

PYTHON = Document("Python (programming language)", "https://en.wikipedia.org/wiki/Python",
                  "A high-level programming language.", 3.0)
MONTY = Document("Monty Python", "https://en.wikipedia.org/wiki/Monty_Python",
                 "A British comedy troupe.", 2.0)


@pytest.fixture
def cache_index(tmp_path):
    """A real, empty wiki cache index that the module-level helpers will pick up."""
    with override_settings(DATA_PATH=str(tmp_path), WIKI_CACHE_INDEX_NAME="wiki-cache.tinysearch",
                           WIKI_CACHE_NUM_PAGES=NUM_PAGES, WIKI_CACHE_ENABLED=True):
        TinyIndex.create(item_factory=Document, index_path=str(wiki_cache_path()),
                         num_pages=NUM_PAGES, page_size=PAGE_SIZE)
        yield str(wiki_cache_path())


def _wiki_api_response(*titles):
    return {"query": {"search": [{"title": title, "snippet": f"<b>{title}</b> snippet"}
                                 for title in titles]}}


def _patched_wikipedia(response):
    """Stand in for the Wikipedia API, returning `response` (or raising, if it is an error)."""
    session = MagicMock()
    if isinstance(response, Exception):
        session.get.side_effect = response
    else:
        session.get.return_value.json.return_value = response
    mock = patch.object(rank.requests, "Session")
    started = mock.start()
    started.return_value.__enter__.return_value = session
    return mock, session


# ---------------------------------------------------------------------------
# The cache term
# ---------------------------------------------------------------------------

def test_cache_term_is_stable_and_normalised():
    """Whitespace and case must not split one query across several entries. The disk cache
    keyed on the raw request URL, so " python " and "Python" missed each other."""
    assert wiki_cache_term("python") == wiki_cache_term("  PYTHON  ")
    assert wiki_cache_term("monty python") == wiki_cache_term("Monty   Python")
    assert wiki_cache_term("python") != wiki_cache_term("monty python")


def test_cache_term_does_not_contain_the_query():
    term = wiki_cache_term("something private")
    assert term.startswith(WIKI_CACHE_TERM_PREFIX)
    assert "something" not in term
    assert "private" not in term


def test_cache_term_is_keyed_so_it_cannot_be_recomputed_without_the_secret():
    """A bare digest of a one-word query is brute-forceable from a wordlist; keyed on
    SECRET_KEY the term->query mapping is not reproducible without the key."""
    with override_settings(SECRET_KEY="one"):
        first = wiki_cache_term("bananas")
    with override_settings(SECRET_KEY="two"):
        second = wiki_cache_term("bananas")

    assert first != second


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_results_come_back_from_the_index(cache_index):
    store_wiki_results("python", [PYTHON, MONTY])

    cached = get_cached_wiki_results("python")

    assert [document.url for document in cached] == [PYTHON.url, MONTY.url]  # descending score
    assert [document.score for document in cached] == [3.0, 2.0]
    assert all(document.state == DocumentState.FROM_WIKI for document in cached)


def test_a_hit_is_indistinguishable_from_a_live_fetch(cache_index):
    """The hashed term is a storage detail. Handing it to the ranker would make a cached
    result differ from the identical one fetched live."""
    store_wiki_results("python", [PYTHON])

    cached = get_cached_wiki_results("python")

    assert [document.term for document in cached] == ["python"]


def test_a_normalised_variant_of_the_query_hits_the_same_entry(cache_index):
    store_wiki_results("Monty Python", [MONTY])

    assert get_cached_wiki_results("  monty   python ") is not None


def test_a_query_we_have_never_asked_about_is_a_miss(cache_index):
    store_wiki_results("python", [PYTHON])

    assert get_cached_wiki_results("something else entirely") is None


def test_the_file_holds_no_query_text(cache_index):
    store_wiki_results("something private", [PYTHON])

    with open(cache_index, "rb") as index_file:
        raw = index_file.read()

    assert b"something private" not in raw


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def test_a_stale_entry_is_not_a_cache_hit(cache_index):
    now = int(time.time())
    store_wiki_results("python", [PYTHON], now=now)

    fresh = get_cached_wiki_results("python", now=now + 60)
    stale = get_cached_wiki_results("python", now=now + 27 * 7 * 24 * 60 * 60)

    assert fresh is not None
    assert stale is None


def test_an_untimestamped_document_is_stale():
    """A document with no last_crawled predates the cache or came from somewhere else;
    treating it as fresh would pin it forever."""
    assert not is_fresh(Document("t", "u", "e", 1.0), int(time.time()))


def test_stale_entries_are_dropped_when_the_page_is_rewritten(cache_index):
    """Expiry is entirely this: a page under write pressure cleans itself, so there is no
    sweep to schedule and nothing accumulates on a page nobody writes to."""
    now = int(time.time())
    old_query, new_query = _colliding_queries(cache_index)
    store_wiki_results(old_query, [PYTHON], now=now)

    much_later = now + 27 * 7 * 24 * 60 * 60
    store_wiki_results(new_query, [MONTY], now=much_later)

    with TinyIndex(Document, cache_index, 'r') as index:
        page = index.get_page(index.get_key_page_index(wiki_cache_term(new_query)))

    assert [document.url for document in page] == [MONTY.url]


# ---------------------------------------------------------------------------
# Negative caching
# ---------------------------------------------------------------------------

def test_a_query_wikipedia_has_nothing_for_is_remembered(cache_index):
    """Without this the query is re-fetched forever - #357 listed it as unfixable, because
    a sentinel in the search index would surface in /raw and in every candidate list."""
    store_wiki_results("nonsense query", [])

    assert get_cached_wiki_results("nonsense query") == []


def test_an_empty_entry_is_not_returned_as_a_result(cache_index):
    store_wiki_results("nonsense query", [])

    cached = get_cached_wiki_results("nonsense query")

    assert [document.url for document in cached] == []
    assert WIKI_CACHE_EMPTY_URL not in [document.url for document in cached]


def test_an_empty_entry_expires_sooner_than_a_real_one(cache_index):
    """An article appearing is a likelier change than an existing one moving."""
    now = int(time.time())
    store_wiki_results("nonsense query", [], now=now)
    store_wiki_results("python", [PYTHON], now=now)

    later = now + 8 * 24 * 60 * 60  # past the negative TTL, well inside the positive one

    assert get_cached_wiki_results("nonsense query", now=later) is None
    assert get_cached_wiki_results("python", now=later) is not None


def test_documents_without_a_url_or_title_are_not_stored(cache_index):
    store_wiki_results("python", [Document("", "", "", 1.0)])

    assert get_cached_wiki_results("python") == []


# ---------------------------------------------------------------------------
# The fetch path
# ---------------------------------------------------------------------------

def test_a_cache_miss_calls_wikipedia_and_stores_the_result(cache_index):
    mock, session = _patched_wikipedia(_wiki_api_response("Python (programming language)"))
    try:
        results = get_wiki_results("python", 3)
    finally:
        mock.stop()

    assert session.get.call_count == 1
    assert [document.url for document in results] == ["https://en.wikipedia.org/wiki/Python_(programming_language)"]
    assert get_cached_wiki_results("python") is not None


def test_a_cache_hit_does_not_call_wikipedia(cache_index):
    store_wiki_results("python", [PYTHON])

    mock, session = _patched_wikipedia(_wiki_api_response("Python"))
    try:
        results = get_wiki_results("python", 3)
    finally:
        mock.stop()

    assert session.get.call_count == 0
    assert [document.url for document in results] == [PYTHON.url]


def test_a_hit_is_served_while_the_circuit_breaker_is_open(cache_index):
    """A cached answer costs Wikipedia nothing, so backing off from a rate limit is no
    reason to withhold it."""
    store_wiki_results("python", [PYTHON])
    rank._trip_wiki_circuit()
    try:
        results = get_wiki_results("python", 3)
    finally:
        rank._wiki_blocked_until = 0.0

    assert [document.url for document in results] == [PYTHON.url]


def test_a_failed_fetch_is_not_remembered_as_an_empty_result(cache_index):
    """Only a well-formed answer from Wikipedia is worth storing. Caching a transient
    failure would suppress the query for a week."""
    mock, _ = _patched_wikipedia(ValueError("connection reset"))
    try:
        assert get_wiki_results("python", 3) == []
    finally:
        mock.stop()

    assert get_cached_wiki_results("python") is None


def test_an_error_response_is_not_remembered_as_an_empty_result(cache_index):
    mock, _ = _patched_wikipedia({"error": {"code": "whatever"}})
    try:
        assert get_wiki_results("python", 3) == []
    finally:
        mock.stop()

    assert get_cached_wiki_results("python") is None


def test_a_broken_cache_write_does_not_break_the_search(cache_index):
    """A cache write that fails costs a re-fetch later. It must not cost a search its
    results."""
    mock, _ = _patched_wikipedia(_wiki_api_response("Python"))
    try:
        with patch("mwmbl.indexer.wiki_cache.TinyIndex", side_effect=OSError("disk gone")):
            results = get_wiki_results("python", 3)
    finally:
        mock.stop()

    assert len(results) == 1


def test_the_query_is_truncated_before_it_reaches_wikipedia(cache_index):
    """MAX_QUERY_CHARS was applied only in HeuristicAndWikiRanker, which is the eval ranker;
    the production LTRRanker path sent the whole thing."""
    mock, session = _patched_wikipedia(_wiki_api_response("Python"))
    try:
        get_wiki_results("x" * 500, 3)
    finally:
        mock.stop()

    requested_url = session.get.call_args[0][0]
    assert "x" * rank.MAX_QUERY_CHARS in requested_url
    assert "x" * (rank.MAX_QUERY_CHARS + 1) not in requested_url


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------

def test_the_kill_switch_stops_reads_and_writes(cache_index):
    with override_settings(WIKI_CACHE_ENABLED=False):
        store_wiki_results("python", [PYTHON])
        assert get_cached_wiki_results("python") is None

    assert get_cached_wiki_results("python") is None


def test_a_missing_cache_file_is_a_miss_rather_than_an_error(tmp_path):
    with override_settings(DATA_PATH=str(tmp_path), WIKI_CACHE_INDEX_NAME="absent.tinysearch",
                           WIKI_CACHE_ENABLED=True):
        assert get_cached_wiki_results("python") is None


# ---------------------------------------------------------------------------
# Page competition
# ---------------------------------------------------------------------------

def _colliding_queries(index_path):
    """Two queries whose cache terms land on the same page."""
    with TinyIndex(Document, index_path, 'r') as index:
        first = "collide-0"
        first_page = index.get_key_page_index(wiki_cache_term(first))
        for i in range(1, 10000):
            candidate = f"collide-{i}"
            if index.get_key_page_index(wiki_cache_term(candidate)) == first_page:
                return first, candidate
    raise AssertionError("No colliding query found")


def _filler(size):
    """Incompressible text. Repeated filler zstd-compresses away and the page never fills."""
    return "".join(random.choices(string.ascii_letters, k=size))


def test_the_newest_entry_survives_a_full_page_and_the_oldest_is_evicted(cache_index):
    """store() drops the tail that does not fit, so entries are written newest-first. That
    ordering is the whole of the eviction policy - and it is deliberately not the ranker's
    ordering, which would evict by relevance instead of by age."""
    old_query, new_query = _colliding_queries(cache_index)
    now = int(time.time())
    bulky = [Document(f"Title {i}", f"https://en.wikipedia.org/wiki/{i}", _filler(2000), 3.0)
             for i in range(4)]

    store_wiki_results(old_query, bulky, now=now)
    assert get_cached_wiki_results(old_query, now=now) is not None

    store_wiki_results(new_query, bulky, now=now + 1)

    assert get_cached_wiki_results(new_query, now=now + 1) is not None
    assert get_cached_wiki_results(old_query, now=now + 1) is None


def test_an_entry_replaces_its_own_previous_results(cache_index):
    store_wiki_results("python", [PYTHON, MONTY])
    store_wiki_results("python", [MONTY])

    assert [document.url for document in get_cached_wiki_results("python")] == [MONTY.url]


# ---------------------------------------------------------------------------
# Keeping Wikipedia off the keystroke path
# ---------------------------------------------------------------------------

class _RecordingRanker(Ranker):
    """The smallest thing that exercises Ranker.search's external_search plumbing, which is
    what LTRRanker inherits and what the production ranker therefore uses."""

    def __init__(self, tiny_index):
        super().__init__(tiny_index, completer=MagicMock(**{"complete.return_value": []}))
        self.external_search_calls = []

    def order_results(self, terms, pages, is_complete):
        return pages

    def external_search(self, q):
        self.external_search_calls.append(q)
        return []


def test_search_does_not_call_external_search_when_it_is_turned_off(cache_index):
    """The search-as-you-type trigger passes use_external_search=False. Typing a
    15-character query otherwise costs a Wikipedia call per keystroke, and 92% of those
    prefixes are ones nobody ever searches for, so no cache can absorb them."""
    with TinyIndex(Document, cache_index, 'r') as index:
        ranker = _RecordingRanker(index)
        ranker.search("python", [], use_external_search=False)

        assert ranker.external_search_calls == []


def test_search_calls_external_search_by_default(cache_index):
    with TinyIndex(Document, cache_index, 'r') as index:
        ranker = _RecordingRanker(index)
        ranker.search("python", [])

        assert ranker.external_search_calls == ["python"]


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def test_a_resized_cache_index_is_rebuilt_rather_than_crashing_startup(tmp_path):
    """Resizing a cache should be a config change, not a startup crash - the cost of
    rebuilding is re-fetching."""
    path = tmp_path / "wiki-cache.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=4, page_size=PAGE_SIZE)

    with override_settings(DATA_PATH=str(tmp_path)):
        create_index("wiki-cache.tinysearch", 16, rebuild_on_mismatch=True)

    with TinyIndex(Document, str(path), 'r') as index:
        assert index.num_pages == 16


def test_a_resized_search_index_still_refuses_to_start(tmp_path):
    """The search index is the opposite case: a size that disagrees with settings means
    somebody changed NUM_PAGES against the only copy of the crawl."""
    path = tmp_path / "index.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=4, page_size=PAGE_SIZE)

    with override_settings(DATA_PATH=str(tmp_path)), pytest.raises(ValueError):
        create_index("index.tinysearch", 16)
