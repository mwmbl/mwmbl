"""Tests for the dedicated external results cache index (mwmbl.indexer.external_cache).

Where these overlap with the tests on the #357 branch they are deliberately the same
assertions - the term derivation and the freshness rule are carried over unchanged. What is
gone is everything that existed only because cache entries shared a file with real index
content: the anonymisation gate, the Redis queue, the general-indexing path, and the guards
stopping a cache term reaching curation or /raw.
"""
import json
import random
import string
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from mwmbl.apps import create_index
from mwmbl.indexer.external_cache import (
    EXTERNAL_CACHE_EMPTY_URL,
    external_cache_path,
    external_cache_term,
    get_cached_external_results,
    is_fresh,
    store_external_results,
)
from mwmbl.tinysearchengine import rank
from mwmbl.tinysearchengine.indexer import (PAGE_SIZE, Document, DocumentSource, DocumentState,
                                            TinyIndex)
from mwmbl.tinysearchengine.rank import (WIKI_FETCH_LIMIT, Ranker, get_wiki_results,
                                          wiki_score)

NUM_PAGES = 8

# state and source are set because get_wiki_results sets them, and the cache preserves what
# the provider produced rather than stamping its own. The scores are not preserved - what
# an entry stores is the rank - and the tests below rely on that.
PYTHON = Document("Python (programming language)", "https://en.wikipedia.org/wiki/Python",
                  "A high-level programming language.", 3.0, state=DocumentState.FROM_WIKI,
                  source=DocumentSource.WIKIPEDIA)
MONTY = Document("Monty Python", "https://en.wikipedia.org/wiki/Monty_Python",
                 "A British comedy troupe.", 2.0, state=DocumentState.FROM_WIKI,
                 source=DocumentSource.WIKIPEDIA)


@pytest.fixture
def cache_index(tmp_path):
    """A real, empty external results cache index that the module-level helpers pick up."""
    with override_settings(DATA_PATH=str(tmp_path), EXTERNAL_CACHE_INDEX_NAME="external-cache.tinysearch",
                           EXTERNAL_CACHE_NUM_PAGES=NUM_PAGES, EXTERNAL_CACHE_ENABLED=True):
        TinyIndex.create(item_factory=Document, index_path=str(external_cache_path()),
                         num_pages=NUM_PAGES, page_size=PAGE_SIZE)
        yield str(external_cache_path())


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
    assert external_cache_term("python") == external_cache_term("  PYTHON  ")
    assert external_cache_term("monty python") == external_cache_term("Monty   Python")
    assert external_cache_term("python") != external_cache_term("monty python")


def test_cache_term_does_not_contain_the_query():
    term = external_cache_term("something private")
    assert "something" not in term
    assert "private" not in term


def test_distinct_queries_do_not_share_a_term():
    """A term collision is silent - one query is answered with another's results - so the
    normalisation must not merge two queries. tokenize() did: it drops the last two tokens
    from anything ending in an ellipsis, and returns [] for whitespace."""
    assert external_cache_term("python asyncio…") != external_cache_term("python tutorial…")
    assert external_cache_term("python asyncio…") != external_cache_term("python")
    assert external_cache_term("   ") != external_cache_term("\t\n x")


def test_the_term_is_derived_from_anything_that_is_a_string():
    """It runs outside the try in get_cached_external_results, so raising here costs a
    search its results rather than a cache lookup."""
    assert external_cache_term("\udce9 lone surrogate")


def test_cache_term_is_keyed_so_it_cannot_be_recomputed_without_the_secret():
    """A bare digest of a one-word query is brute-forceable from a wordlist; keyed on
    SECRET_KEY the term->query mapping is not reproducible without the key."""
    with override_settings(SECRET_KEY="one"):
        first = external_cache_term("bananas")
    with override_settings(SECRET_KEY="two"):
        second = external_cache_term("bananas")

    assert first != second


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_results_come_back_from_the_index(cache_index):
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON, MONTY])

    cached = get_cached_external_results(DocumentSource.WIKIPEDIA, "python")

    assert [document.url for document in cached] == [PYTHON.url, MONTY.url]
    assert all(document.state == DocumentState.FROM_WIKI for document in cached)


def test_the_providers_order_is_what_comes_back(cache_index):
    """The cache stores the rank the provider gave, not the score it was handed. Storing
    the score would make an entry mean different things to callers asking for different
    numbers of results - see wiki_score."""
    store_external_results(DocumentSource.WIKIPEDIA, "python", [MONTY, PYTHON])

    cached = get_cached_external_results(DocumentSource.WIKIPEDIA, "python")

    assert [document.url for document in cached] == [MONTY.url, PYTHON.url]


def test_a_cached_result_carries_no_score(cache_index):
    """Scoring is the caller's: a rank is what the provider said, a score is on a scale the
    cache does not know."""
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON, MONTY])

    cached = get_cached_external_results(DocumentSource.WIKIPEDIA, "python")

    assert [document.score for document in cached] == [None, None]


def test_a_hit_is_indistinguishable_from_a_live_fetch(cache_index):
    """The hashed term is a storage detail. Handing it to the ranker would make a cached
    result differ from the identical one fetched live."""
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])

    cached = get_cached_external_results(DocumentSource.WIKIPEDIA, "python")

    assert [document.term for document in cached] == ["python"]


def test_a_normalised_variant_of_the_query_hits_the_same_entry(cache_index):
    store_external_results(DocumentSource.WIKIPEDIA, "Monty Python", [MONTY])

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "  monty   python ") is not None


def test_a_query_we_have_never_asked_about_is_a_miss(cache_index):
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "something else entirely") is None


def _all_stored_documents(index_path):
    """Every document in the file, decompressed. Grepping the raw bytes proves little: the
    pages are zstd-compressed, so plaintext would not show up there either way."""
    with TinyIndex(Document, index_path, 'r') as index:
        return [document for page in range(index.num_pages) for document in index.get_page(page)]


def test_the_file_holds_no_query_text(cache_index):
    store_external_results(DocumentSource.WIKIPEDIA, "something private", [PYTHON])

    stored = _all_stored_documents(cache_index)

    assert stored, "nothing was written, so this would pass for the wrong reason"
    written = json.dumps([document.as_tuple() for document in stored])
    assert "something private" not in written
    assert "private" not in written


# ---------------------------------------------------------------------------
# Several providers sharing the file
# ---------------------------------------------------------------------------

# The second provider is the whole reason the cache is keyed by source rather than by query
# alone, so the tests below exercise it even though nothing fetches from it yet.
OTHER_SOURCE = DocumentSource.STAAN

STAAN_RESULT = Document("Python tutorial", "https://example.com/python",
                        "Somewhere that is not Wikipedia.", 9.0, source=DocumentSource.STAAN)


def test_two_providers_do_not_share_an_entry_for_the_same_query(cache_index):
    """Providers share the term, and therefore the page, but not the entry: one provider
    answering a query must not stop the other being asked, or answer on its behalf."""
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])

    assert get_cached_external_results(OTHER_SOURCE, "python") is None

    store_external_results(OTHER_SOURCE, "python", [STAAN_RESULT])

    assert [d.url for d in get_cached_external_results(DocumentSource.WIKIPEDIA, "python")] == [PYTHON.url]
    assert [d.url for d in get_cached_external_results(OTHER_SOURCE, "python")] == [STAAN_RESULT.url]


def test_an_entry_says_which_provider_it_came_from_but_not_which_query(cache_index):
    """An entry has to be attributable to its provider - to count them, to drop one
    provider's entries, or to give one its own TTL - which the query must never be. The
    source is on the document; the query is not anywhere."""
    store_external_results(DocumentSource.WIKIPEDIA, "something private", [PYTHON])

    stored = _all_stored_documents(cache_index)

    assert [document for document in stored if document.source == DocumentSource.WIKIPEDIA]
    assert not any("private" in document.term for document in stored)


def test_storing_one_provider_does_not_evict_another_for_the_same_query(cache_index):
    """The trap in sharing a term. The write path replaces "this query's entries", and if
    that means the term alone it wipes every other provider's results for the query - with
    no error, just a silent extra fetch every time."""
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])
    store_external_results(OTHER_SOURCE, "python", [STAAN_RESULT])
    store_external_results(DocumentSource.WIKIPEDIA, "python", [MONTY])

    assert [d.url for d in get_cached_external_results(OTHER_SOURCE, "python")] == [STAAN_RESULT.url]
    assert [d.url for d in get_cached_external_results(DocumentSource.WIKIPEDIA, "python")] == [MONTY.url]


def test_both_providers_results_for_a_query_live_on_one_page(cache_index):
    """The reason the term does not name the source: one page read serves every provider,
    so asking the second costs no extra page fault."""
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])
    store_external_results(OTHER_SOURCE, "python", [STAAN_RESULT])

    with TinyIndex(Document, cache_index, 'r') as index:
        page = index.get_page(index.get_key_page_index(external_cache_term("python")))

    assert {document.source for document in page} == {DocumentSource.WIKIPEDIA, DocumentSource.STAAN}


def test_the_negative_sentinel_carries_its_source_too(cache_index):
    """is_fresh reads document.source off a page's other entries, where the term tells it
    nothing, so an entry with no results still has to say where it came from."""
    store_external_results(DocumentSource.WIKIPEDIA, "nonsense query", [])

    stored = _all_stored_documents(cache_index)

    assert [document.source for document in stored] == [DocumentSource.WIKIPEDIA]


def test_a_providers_state_and_source_survive_the_round_trip(cache_index):
    """The cache preserves what the provider produced rather than stamping its own, so a
    cached result stays attributable once there is a second provider."""
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])

    cached = get_cached_external_results(DocumentSource.WIKIPEDIA, "python")

    assert [document.state for document in cached] == [DocumentState.FROM_WIKI]
    assert [document.source for document in cached] == [DocumentSource.WIKIPEDIA]


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def test_a_stale_entry_is_not_a_cache_hit(cache_index):
    now = int(time.time())
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON], now=now)

    fresh = get_cached_external_results(DocumentSource.WIKIPEDIA, "python", now=now + 60)
    stale = get_cached_external_results(DocumentSource.WIKIPEDIA, "python", now=now + 27 * 7 * 24 * 60 * 60)

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
    store_external_results(DocumentSource.WIKIPEDIA, old_query, [PYTHON], now=now)

    much_later = now + 27 * 7 * 24 * 60 * 60
    store_external_results(DocumentSource.WIKIPEDIA, new_query, [MONTY], now=much_later)

    with TinyIndex(Document, cache_index, 'r') as index:
        page = index.get_page(index.get_key_page_index(external_cache_term(new_query)))

    assert [document.url for document in page] == [MONTY.url]


# ---------------------------------------------------------------------------
# Negative caching
# ---------------------------------------------------------------------------

def test_a_query_a_provider_has_nothing_for_is_remembered(cache_index):
    """Without this the query is re-fetched forever - #357 listed it as unfixable, because
    a sentinel in the search index would surface in /raw and in every candidate list."""
    store_external_results(DocumentSource.WIKIPEDIA, "nonsense query", [])

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "nonsense query") == []


def test_an_empty_entry_is_not_returned_as_a_result(cache_index):
    store_external_results(DocumentSource.WIKIPEDIA, "nonsense query", [])

    cached = get_cached_external_results(DocumentSource.WIKIPEDIA, "nonsense query")

    assert [document.url for document in cached] == []
    assert EXTERNAL_CACHE_EMPTY_URL not in [document.url for document in cached]


def test_one_providers_empty_result_is_not_anothers(cache_index):
    """Sharing a page makes this worth pinning down: "Wikipedia has nothing" must not read
    as "Staan has nothing", or the second provider is never asked."""
    store_external_results(DocumentSource.WIKIPEDIA, "nonsense query", [])

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "nonsense query") == []
    assert get_cached_external_results(OTHER_SOURCE, "nonsense query") is None


def test_an_empty_entry_expires_sooner_than_a_real_one(cache_index):
    """An article appearing is a likelier change than an existing one moving."""
    now = int(time.time())
    store_external_results(DocumentSource.WIKIPEDIA, "nonsense query", [], now=now)
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON], now=now)

    later = now + 8 * 24 * 60 * 60  # past the negative TTL, well inside the positive one

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "nonsense query", now=later) is None
    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "python", now=later) is not None


def test_documents_without_a_url_or_title_are_not_stored(cache_index):
    store_external_results(DocumentSource.WIKIPEDIA, "python", [Document("", "", "", 1.0)])

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "python") == []


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
    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "python") is not None


def test_a_cache_hit_does_not_call_wikipedia(cache_index):
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])

    mock, session = _patched_wikipedia(_wiki_api_response("Python"))
    try:
        results = get_wiki_results("python", 3)
    finally:
        mock.stop()

    assert session.get.call_count == 0
    assert [document.url for document in results] == [PYTHON.url]
    assert [document.score for document in results] == [wiki_score(0)]


def test_wikipedia_is_asked_for_a_fixed_number_of_results(cache_index):
    """Not the caller's max: what comes back is what the entry holds, and an entry warmed
    by a caller wanting three must not be a short answer for a caller wanting five."""
    mock, session = _patched_wikipedia(_wiki_api_response("Python"))
    try:
        get_wiki_results("python", 2)
    finally:
        mock.stop()

    assert f"srlimit={WIKI_FETCH_LIMIT}" in session.get.call_args[0][0]


def test_a_short_request_does_not_leave_a_short_entry(cache_index):
    """The whole response is stored and the caller's cut taken afterwards. Storing only
    what the first caller wanted would silently short-change every later one for the length
    of the TTL - a cache miss is visible, a truncated hit is not."""
    titles = [f"Title {i}" for i in range(WIKI_FETCH_LIMIT)]
    mock, _ = _patched_wikipedia(_wiki_api_response(*titles))
    try:
        assert len(get_wiki_results("python", 2)) == 2
    finally:
        mock.stop()

    mock, session = _patched_wikipedia(_wiki_api_response(*titles))
    try:
        results = get_wiki_results("python", WIKI_FETCH_LIMIT)
    finally:
        mock.stop()

    assert session.get.call_count == 0
    assert len(results) == WIKI_FETCH_LIMIT


def test_a_results_score_does_not_depend_on_how_many_were_asked_for(cache_index):
    """`score` is a feature of the LTR model. When it was max_wiki_results + 1 - i the same
    Wikipedia result scored 6 for a caller wanting five and 4 for a caller wanting three,
    so how many results a caller asked for moved the ranking of the ones it got."""
    titles = [f"Title {i}" for i in range(WIKI_FETCH_LIMIT)]
    mock, _ = _patched_wikipedia(_wiki_api_response(*titles))
    try:
        three = get_wiki_results("python", 3)
        five = get_wiki_results("python", 5)
    finally:
        mock.stop()

    assert [document.score for document in three] == [wiki_score(0), wiki_score(1), wiki_score(2)]
    assert [document.score for document in five[:3]] == [document.score for document in three]


def test_a_cache_hit_scores_the_same_as_the_live_fetch_that_filled_it(cache_index):
    """The scores are derived on the way out rather than stored, so this is the assertion
    that keeps the derivation honest."""
    titles = [f"Title {i}" for i in range(WIKI_FETCH_LIMIT)]
    mock, _ = _patched_wikipedia(_wiki_api_response(*titles))
    try:
        live = get_wiki_results("python", 3)
    finally:
        mock.stop()

    cached = get_wiki_results("python", 3)

    assert [(document.url, document.score) for document in cached] == \
           [(document.url, document.score) for document in live]


def test_a_hit_is_served_while_the_circuit_breaker_is_open(cache_index):
    """A cached answer costs Wikipedia nothing, so backing off from a rate limit is no
    reason to withhold it."""
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])
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

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "python") is None


def test_an_error_response_is_not_remembered_as_an_empty_result(cache_index):
    mock, _ = _patched_wikipedia({"error": {"code": "whatever"}})
    try:
        assert get_wiki_results("python", 3) == []
    finally:
        mock.stop()

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "python") is None


def test_a_broken_cache_write_does_not_break_the_search(cache_index):
    """A cache write that fails costs a re-fetch later. It must not cost a search its
    results."""
    mock, _ = _patched_wikipedia(_wiki_api_response("Python"))
    try:
        with patch("mwmbl.indexer.external_cache.TinyIndex", side_effect=OSError("disk gone")):
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
    with override_settings(EXTERNAL_CACHE_ENABLED=False):
        store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON])
        assert get_cached_external_results(DocumentSource.WIKIPEDIA, "python") is None

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, "python") is None


def test_a_missing_cache_file_is_a_miss_rather_than_an_error(tmp_path):
    with override_settings(DATA_PATH=str(tmp_path), EXTERNAL_CACHE_INDEX_NAME="absent.tinysearch",
                           EXTERNAL_CACHE_ENABLED=True):
        assert get_cached_external_results(DocumentSource.WIKIPEDIA, "python") is None


# ---------------------------------------------------------------------------
# Page competition
# ---------------------------------------------------------------------------

def _colliding_queries(index_path):
    """Two queries whose cache terms land on the same page."""
    with TinyIndex(Document, index_path, 'r') as index:
        first = "collide-0"
        first_page = index.get_key_page_index(external_cache_term(first))
        for i in range(1, 10000):
            candidate = f"collide-{i}"
            if index.get_key_page_index(external_cache_term(candidate)) == first_page:
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

    store_external_results(DocumentSource.WIKIPEDIA, old_query, bulky, now=now)
    assert get_cached_external_results(DocumentSource.WIKIPEDIA, old_query, now=now) is not None

    store_external_results(DocumentSource.WIKIPEDIA, new_query, bulky, now=now + 1)

    assert get_cached_external_results(DocumentSource.WIKIPEDIA, new_query, now=now + 1) is not None
    assert get_cached_external_results(DocumentSource.WIKIPEDIA, old_query, now=now + 1) is None


def test_an_entry_replaces_its_own_previous_results(cache_index):
    store_external_results(DocumentSource.WIKIPEDIA, "python", [PYTHON, MONTY])
    store_external_results(DocumentSource.WIKIPEDIA, "python", [MONTY])

    assert [document.url for document in get_cached_external_results(DocumentSource.WIKIPEDIA, "python")] == [MONTY.url]


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
    path = tmp_path / "external-cache.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=4, page_size=PAGE_SIZE)

    with override_settings(DATA_PATH=str(tmp_path)):
        create_index("external-cache.tinysearch", 16, rebuild_on_mismatch=True)

    with TinyIndex(Document, str(path), 'r') as index:
        assert index.num_pages == 16


def test_a_resized_search_index_still_refuses_to_start(tmp_path):
    """The search index is the opposite case: a size that disagrees with settings means
    somebody changed NUM_PAGES against the only copy of the crawl."""
    path = tmp_path / "index.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=4, page_size=PAGE_SIZE)

    with override_settings(DATA_PATH=str(tmp_path)), pytest.raises(ValueError):
        create_index("index.tinysearch", 16)


def test_an_unreadable_cache_index_is_rebuilt_rather_than_crashing_startup(tmp_path):
    """A file that does not parse as an index at all is rejected before there is a page
    size to compare, so it never reaches the size check. For a disposable index it is the
    same situation: unusable contents, cheap to rebuild."""
    path = tmp_path / "external-cache.tinysearch"
    path.write_bytes(b"not an index" + bytes(PAGE_SIZE))

    with override_settings(DATA_PATH=str(tmp_path)):
        create_index("external-cache.tinysearch", 16, rebuild_on_mismatch=True)

    with TinyIndex(Document, str(path), 'r') as index:
        assert index.num_pages == 16


def test_an_unreadable_search_index_still_refuses_to_start(tmp_path):
    path = tmp_path / "index.tinysearch"
    path.write_bytes(b"not an index" + bytes(PAGE_SIZE))

    with override_settings(DATA_PATH=str(tmp_path)), pytest.raises(ValueError):
        create_index("index.tinysearch", 16)


def test_a_rebuild_leaves_no_window_with_the_index_missing(tmp_path):
    """The replacement is built alongside and renamed over the old file. unlink() then
    create() leaves a gap where the path does not exist, and on a deploy sharing /data the
    outgoing container goes on writing to the unlinked inode."""
    path = tmp_path / "external-cache.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=4, page_size=PAGE_SIZE)
    original_inode = path.stat().st_ino
    seen = []

    real_create = TinyIndex.create

    def watched_create(*args, **kwargs):
        seen.append((path.exists(), kwargs.get("index_path", args[1] if len(args) > 1 else None)))
        return real_create(*args, **kwargs)

    with override_settings(DATA_PATH=str(tmp_path)), \
            patch.object(TinyIndex, "create", side_effect=watched_create):
        create_index("external-cache.tinysearch", 16, rebuild_on_mismatch=True)

    assert seen, "nothing was rebuilt, so this would pass for the wrong reason"
    assert all(existed for existed, _ in seen), "the old index was removed before the new one existed"
    assert all(Path(str(target)) != path for _, target in seen), "the new index was written over the old one in place"
    assert path.stat().st_ino != original_inode
    assert not list(tmp_path.glob("*.rebuild"))
