"""Wikipedia results cached in the index instead of in a filesystem HTTP cache.

Three things are under test: the query never reaches the index in readable form, a query's
results come back from the index instead of from Wikipedia, and a Wikipedia page we have
not seen before becomes an ordinary index entry rather than a query-specific one.
"""
import time
from random import Random
from string import ascii_letters
from unittest.mock import MagicMock, patch

import fakeredis
import pytest
from django.test import override_settings

from mwmbl import background
from mwmbl.format import format_result, format_result_v2
from mwmbl.indexer import wiki_cache
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist
from mwmbl.indexer.index import tokenize_document
from mwmbl.indexer.index_batches import index_pages, preprocess_documents
from mwmbl.indexer.wiki_cache import (
    WIKI_CACHE_TERM_PREFIX, drain_wiki_queue, drop_expired_wiki_cache, enqueue_wiki_results,
    get_cached_wiki_results, unseen_wiki_urls, wiki_cache_term, wiki_terms_for_documents,
)
from mwmbl.tinysearchengine import rank
from mwmbl.tinysearchengine.indexer import Document, DocumentState, PAGE_SIZE, TinyIndex
from mwmbl.tinysearchengine.rank import (
    HeuristicAndWikiRanker, HeuristicRanker, get_wiki_intro_extracts, score_result,
)

NUM_PAGES = 64

# A result that contains its query words, so it can take real query terms.
PYTHON = Document(title="Python (programming language)",
                  url="https://en.wikipedia.org/wiki/Python_(programming_language)",
                  extract="Python is a high-level programming language.",
                  score=3.0, state=DocumentState.FROM_WIKI)
# A spelling-corrected result: Wikipedia returned it for "pithon", which appears nowhere
# in it, so it may only ever be reachable through the query hash.
CORRECTED = Document(title="Monty Python", url="https://en.wikipedia.org/wiki/Monty_Python",
                     extract="A British comedy troupe.",
                     score=2.0, state=DocumentState.FROM_WIKI)


@pytest.fixture
def index_path(tmp_path):
    path = str(tmp_path / "wiki.tinysearch")
    TinyIndex.create(item_factory=Document, index_path=path, num_pages=NUM_PAGES, page_size=PAGE_SIZE)
    return path


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


def write_queue_to_index(index_path, redis_client, limit=1000):
    """Do what the background task does: drain the queue and write it into the index."""
    documents = drain_wiki_queue(limit, redis_client)
    page_documents = {}
    with TinyIndex(Document, index_path, 'r') as index:
        for document in documents:
            page_documents.setdefault(index.get_key_page_index(document.term), []).append(document)
    for documents_for_page in page_documents.values():
        documents_for_page.sort(key=lambda document: -(document.score or 0.0))
    index_pages(index_path, page_documents)
    return documents


def terms_written(query, documents):
    return {document.term for document in wiki_terms_for_documents(query, documents)}


# ---------------------------------------------------------------------------
# The cache term
# ---------------------------------------------------------------------------

def test_cache_term_is_stable_and_normalised():
    assert wiki_cache_term("Python") == wiki_cache_term(" python ") == wiki_cache_term("python")
    assert wiki_cache_term("python").startswith(WIKI_CACHE_TERM_PREFIX)


def test_cache_term_does_not_contain_the_query():
    term = wiki_cache_term("bananas")

    assert "banana" not in term
    assert wiki_cache_term("bananas") != wiki_cache_term("apples")


def test_cache_term_is_keyed_so_it_cannot_be_recomputed_without_the_secret():
    """A bare digest of a one-word query is brute-forceable from a wordlist; keyed on
    SECRET_KEY the term->query mapping is not reproducible without the key."""
    with override_settings(SECRET_KEY="one"):
        first = wiki_cache_term("bananas")
    with override_settings(SECRET_KEY="two"):
        second = wiki_cache_term("bananas")

    assert first != second


# ---------------------------------------------------------------------------
# The anonymisation gate
# ---------------------------------------------------------------------------

def test_a_one_word_query_present_in_the_document_takes_a_real_term():
    terms = terms_written("python", [PYTHON])

    assert terms == {"python", wiki_cache_term("python")}


def test_a_two_word_query_takes_both_unigrams_and_the_bigram():
    terms = terms_written("python programming", [PYTHON])

    assert terms == {"python", "programming", "python programming",
                     wiki_cache_term("python programming")}


def test_a_document_missing_a_query_word_only_gets_the_hash():
    """Wikipedia spelling-corrects and partial-matches, so a returned document need not
    contain the query at all. Filing it under a word it does not contain would put a
    user's query into the index."""
    terms = terms_written("pithon", [CORRECTED])

    assert terms == {wiki_cache_term("pithon")}


def test_a_two_word_query_matching_only_one_word_only_gets_the_hash():
    terms = terms_written("python bananas", [PYTHON])

    assert terms == {wiki_cache_term("python bananas")}


@override_settings(WIKI_CACHE_MAX_ORGANIC_TERM_TOKENS=2)
def test_a_three_word_query_only_gets_the_hash_even_when_every_word_matches():
    query = "python is programming"
    assert set(query.split()) <= wiki_cache.document_token_set(PYTHON)

    assert terms_written(query, [PYTHON]) == {wiki_cache_term(query)}


def test_a_mixed_result_set_gates_per_document():
    terms = terms_written("python", [PYTHON, CORRECTED])

    # Both are cached; only the one containing "python" is reachable by that word.
    assert terms == {"python", wiki_cache_term("python")}
    cached = [d for d in wiki_terms_for_documents("python", [PYTHON, CORRECTED])
              if d.term == wiki_cache_term("python")]
    assert {d.url for d in cached} == {PYTHON.url, CORRECTED.url}


def test_documents_without_a_title_or_url_are_not_stored():
    assert wiki_terms_for_documents("python", [Document(title="", url="", extract="x")]) == []


# ---------------------------------------------------------------------------
# Round trip through the queue and the index
# ---------------------------------------------------------------------------

def test_results_come_back_from_the_index(index_path, redis_client):
    enqueue_wiki_results("python", [PYTHON, CORRECTED], redis_client)
    write_queue_to_index(index_path, redis_client)

    with TinyIndex(Document, index_path, 'r') as index:
        cached = get_cached_wiki_results(index, "python")

    assert [d.url for d in cached] == [PYTHON.url, CORRECTED.url]   # descending score
    assert all(d.state == DocumentState.FROM_WIKI for d in cached)
    assert [d.score for d in cached] == [3.0, 2.0]


def test_the_queue_never_holds_the_query(redis_client):
    enqueue_wiki_results("bananas", [CORRECTED], redis_client)

    queued = redis_client.smembers(wiki_cache.WIKI_CACHE_QUEUE_KEY)

    assert queued
    assert not any("banana" in payload for payload in queued)


def test_a_qualifying_result_is_retrievable_by_its_own_word(index_path, redis_client):
    enqueue_wiki_results("python", [PYTHON], redis_client)
    write_queue_to_index(index_path, redis_client)

    with TinyIndex(Document, index_path, 'r') as index:
        urls = {d.url for d in index.retrieve("python")}

    assert urls == {PYTHON.url}


def test_a_corrected_result_is_not_retrievable_by_the_query_word(index_path, redis_client):
    enqueue_wiki_results("pithon", [CORRECTED], redis_client)
    write_queue_to_index(index_path, redis_client)

    with TinyIndex(Document, index_path, 'r') as index:
        assert index.retrieve("pithon") == []
        assert {d.url for d in get_cached_wiki_results(index, "pithon")} == {CORRECTED.url}


def test_hash_term_order_survives_an_arbitrary_queue_order(index_path, redis_client):
    """The queue is a SET drained with SPOP, and under the hash term the write path scores
    every document against a term none of them match, so nothing but the score orders
    them."""
    # Enqueued worst-score-first, and SPOP will reorder them again anyway.
    enqueue_wiki_results("pithon", [CORRECTED, PYTHON], redis_client)
    write_queue_to_index(index_path, redis_client)

    with TinyIndex(Document, index_path, 'r') as index:
        cached = get_cached_wiki_results(index, "pithon")

    assert [d.score for d in cached] == [3.0, 2.0]


def test_enqueueing_the_same_results_twice_adds_nothing(redis_client):
    first = enqueue_wiki_results("python", [PYTHON], redis_client)
    second = enqueue_wiki_results("python", [PYTHON], redis_client)

    assert first > 0
    assert second == 0


def test_enqueueing_survives_a_broken_redis():
    """Nothing here may affect a search response; a lost entry costs one re-fetch."""
    broken = MagicMock()
    broken.scard.side_effect = RuntimeError("redis is down")

    assert enqueue_wiki_results("python", [PYTHON], broken) == 0


@override_settings(WIKI_CACHE_ENABLED=False)
def test_the_kill_switch_stops_reads_and_writes(index_path, redis_client):
    assert enqueue_wiki_results("python", [PYTHON], redis_client) == 0
    with TinyIndex(Document, index_path, 'r') as index:
        assert get_cached_wiki_results(index, "python") == []


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def test_a_stale_entry_is_not_a_cache_hit(index_path, redis_client):
    enqueue_wiki_results("python", [PYTHON], redis_client)
    write_queue_to_index(index_path, redis_client)

    with TinyIndex(Document, index_path, 'r') as index:
        stale = int(time.time()) + 2 * 10 * 7 * 24 * 60 * 60
        assert get_cached_wiki_results(index, "python") != []
        assert get_cached_wiki_results(index, "python", now=stale) == []


def test_stale_cache_entries_are_dropped_from_a_page_being_rewritten():
    now = int(time.time())
    fresh = Document(title="a", url="https://a.test", extract="", term=wiki_cache_term("a"),
                     state=DocumentState.FROM_WIKI, last_crawled=now)
    stale = Document(title="b", url="https://b.test", extract="", term=wiki_cache_term("b"),
                     state=DocumentState.FROM_WIKI, last_crawled=now - 10 * 7 * 24 * 60 * 60 - 1)
    # Filed under its own token rather than a hash: index content, not cache, so it stays
    # however old it is.
    organic = Document(title="c", url="https://c.test", extract="", term="c",
                       state=DocumentState.FROM_WIKI, last_crawled=1)

    kept = drop_expired_wiki_cache([fresh, stale, organic], now=now)

    assert [d.url for d in kept] == [fresh.url, organic.url]


# ---------------------------------------------------------------------------
# The read path
# ---------------------------------------------------------------------------

class TermIndex:
    """An index that answers per term, unlike the fake in test_rank_blacklist."""

    def __init__(self, documents_by_term=None):
        self.documents_by_term = documents_by_term or {}

    def retrieve(self, key):
        return list(self.documents_by_term.get(key, []))


class TrackingRanker(HeuristicRanker):
    def __init__(self, documents_by_term=None):
        completer = MagicMock()
        completer.complete.return_value = []
        super().__init__(TermIndex(documents_by_term), completer)
        self.external_search_calls = []

    def external_search(self, q):
        self.external_search_calls.append(q)
        return [PYTHON]


def cached_page(query, documents):
    term = wiki_cache_term(query)
    return {term: [Document(title=d.title, url=d.url, extract=d.extract, score=d.score,
                            term=term, state=DocumentState.FROM_WIKI,
                            last_crawled=int(time.time()))
                   for d in documents]}


def test_a_cache_hit_does_not_call_wikipedia(redis_client):
    ranker = TrackingRanker(cached_page("python", [PYTHON]))

    with patch.object(wiki_cache, "get_redis", return_value=redis_client):
        results = ranker.search("python", [])

    assert ranker.external_search_calls == []
    assert PYTHON.url in [r.url for r in results]


def test_a_cache_miss_calls_wikipedia_and_queues_the_results(redis_client):
    ranker = TrackingRanker()

    with patch.object(wiki_cache, "get_redis", return_value=redis_client):
        ranker.search("python", [])

    assert ranker.external_search_calls == ["python"]
    assert redis_client.scard(wiki_cache.WIKI_CACHE_QUEUE_KEY) > 0


def test_autocomplete_neither_fetches_nor_caches(redis_client):
    ranker = TrackingRanker(cached_page("python", [PYTHON]))

    with patch.object(wiki_cache, "get_redis", return_value=redis_client):
        ranker.complete("python")

    assert ranker.external_search_calls == []
    assert redis_client.scard(wiki_cache.WIKI_CACHE_QUEUE_KEY) == 0


def test_a_cached_result_is_ranked_rather_than_pinned_as_curated(redis_client):
    """A one-word query stores its results under the curation term with a non-None state.
    Treating those as curated would put them above everything, ranking skipped."""
    better = Document(title="python", url="https://short.test", extract="python python",
                      score=1.0, term="python")
    documents = cached_page("python", [PYTHON])
    documents["python"] = [PYTHON, better]

    ranker = TrackingRanker(documents)
    with patch.object(wiki_cache, "get_redis", return_value=redis_client):
        results = ranker.search("python", [])

    scored = [(score_result(["python"], d, True), d.url) for d in (PYTHON, better)]
    expected_first = max(scored)[1]
    assert results[0].url == expected_first


def test_an_approved_wiki_result_is_still_curated(redis_client):
    approved = Document(title="Python", url="https://en.wikipedia.org/wiki/Python",
                        extract="", score=0.0, term="python",
                        state=DocumentState.FROM_WIKI_APPROVED)
    better = Document(title="python", url="https://short.test", extract="python python",
                      score=1.0, term="python")

    ranker = TrackingRanker({"python": [better, approved]})
    with patch.object(wiki_cache, "get_redis", return_value=redis_client):
        results = ranker.search("python", [])

    assert results[0].url == approved.url


def test_a_query_that_looks_like_a_cache_term_reads_nothing(redis_client):
    term = wiki_cache_term("secret query")
    ranker = TrackingRanker({term: [Document(title="Leaked", url="https://leaked.test",
                                             extract="", term=term,
                                             state=DocumentState.FROM_WIKI,
                                             last_crawled=int(time.time()))]})

    with patch.object(wiki_cache, "get_redis", return_value=redis_client):
        results = ranker.search(term, [])
        raw = ranker.get_raw_results(term)

    assert "https://leaked.test" not in [r.url for r in results]
    assert raw == []


# ---------------------------------------------------------------------------
# The standard indexing path
# ---------------------------------------------------------------------------

def test_a_new_url_is_indexed_under_its_own_tokens(index_path):
    """The point of the general path: the page is findable by words from the article
    itself, not only by the query that happened to surface it."""
    index_pages(index_path, preprocess_documents([PYTHON], index_path))

    with TinyIndex(Document, index_path, 'r') as index:
        assert {d.url for d in index.retrieve("high-level")} == {PYTHON.url}


def test_the_general_path_keeps_the_wikipedia_source(index_path):
    index_pages(index_path, preprocess_documents([PYTHON], index_path))

    with TinyIndex(Document, index_path, 'r') as index:
        stored = index.retrieve("high-level")

    assert [d.state for d in stored] == [DocumentState.FROM_WIKI]
    assert format_result(stored[0], "python")['source'] == "wikipedia"
    assert format_result_v2(stored[0], 1, "python")['engine'] == "wikipedia"


def test_a_url_is_only_returned_as_unseen_once(redis_client):
    first = unseen_wiki_urls([PYTHON, CORRECTED], 10, redis_client)
    second = unseen_wiki_urls([PYTHON, CORRECTED], 10, redis_client)

    assert {d.url for d in first} == {PYTHON.url, CORRECTED.url}
    assert second == []


def test_urls_beyond_the_limit_are_left_for_the_next_run(redis_client):
    """They must not be recorded as seen: that would mark them done and never index them."""
    first = unseen_wiki_urls([PYTHON, CORRECTED], 1, redis_client)
    second = unseen_wiki_urls([PYTHON, CORRECTED], 1, redis_client)

    assert len(first) == len(second) == 1
    assert {d.url for d in first + second} == {PYTHON.url, CORRECTED.url}


def test_a_weakly_matching_wiki_document_scores_zero_like_an_organic_one():
    """FROM_WIKI is not curation, so it gets no exemption from the minimum-term-match
    rule - at query time or in sort_documents' write-time page ordering."""
    terms = ["python", "bananas", "oranges", "apples"]
    organic = Document(title=PYTHON.title, url=PYTHON.url, extract=PYTHON.extract)

    assert score_result(terms, PYTHON, True) == score_result(terms, organic, True) == 0.0


# ---------------------------------------------------------------------------
# Page competition
# ---------------------------------------------------------------------------

def test_a_cache_entry_survives_a_full_page_and_costs_the_deepest_results(index_path):
    """sort_documents interleaves terms by rank position, so a new term's documents sit in
    the first rounds and what falls off the end is the tail of the longest term."""
    cache_term = wiki_cache_term("python")
    with TinyIndex(Document, index_path, 'r') as index:
        page = index.get_key_page_index(cache_term)
        # A term that shares the cache term's page, so the two really do compete.
        crowded = next(f"crowded{i}" for i in range(10000)
                       if index.get_key_page_index(f"crowded{i}") == page)

    # Incompressible extracts: repeated filler zstd-compresses away and the page never
    # fills, which is the whole point of the test.
    crowd = [Document(title=f"Crowd {i}", url=f"https://crowd.test/{i}",
                      extract="".join(Random(i).choices(ascii_letters, k=200)),
                      score=1.0, term=crowded)
             for i in range(200)]
    index_pages(index_path, {page: crowd})

    with TinyIndex(Document, index_path, 'r') as index:
        before = index.get_page(page)
    assert 0 < len(before) < len(crowd), "expected the page to be full"

    cached = [Document(title=PYTHON.title, url=PYTHON.url, extract=PYTHON.extract,
                       score=PYTHON.score, term=cache_term,
                       state=DocumentState.FROM_WIKI, last_crawled=int(time.time()))]
    index_pages(index_path, {page: cached})

    with TinyIndex(Document, index_path, 'r') as index:
        after = index.get_page(page)

    assert PYTHON.url in [d.url for d in after], "the cache entry must survive a full page"
    surviving_crowd = [d for d in after if d.term == crowded]
    assert len(surviving_crowd) < len(before), "and it costs the deepest existing results"


# ---------------------------------------------------------------------------
# The intro extract
# ---------------------------------------------------------------------------

WIKI_EXTRACTS_RESPONSE = {
    "query": {"pages": {
        "23862": {"title": "Python (programming language)",
                  "extract": "Python is a high-level, general-purpose programming language."},
        "18942": {"title": "Monty Python", "extract": ""},
    }},
}


def test_intro_extracts_are_keyed_by_title():
    session = MagicMock()
    session.get.return_value.json.return_value = WIKI_EXTRACTS_RESPONSE

    with patch("mwmbl.tinysearchengine.rank.requests.Session") as mock_session:
        mock_session.return_value.__enter__.return_value = session
        extracts = get_wiki_intro_extracts(["Python (programming language)", "Monty Python"])

    # An empty extract is no better than the snippet, so it is not returned.
    assert extracts == {"Python (programming language)":
                        "Python is a high-level, general-purpose programming language."}


def test_intro_extracts_do_not_call_wikipedia_with_the_circuit_open():
    rank._trip_wiki_circuit()
    try:
        with patch("mwmbl.tinysearchengine.rank.requests.Session") as mock_session:
            assert get_wiki_intro_extracts(["Python"]) == {}
        mock_session.assert_not_called()
    finally:
        rank._wiki_blocked_until = 0.0


def test_the_intro_replaces_the_query_chosen_snippet():
    """The snippet Wikipedia returns is the passage that matched the query. Keeping it as
    the permanent extract would put a query-shaped artefact in the index - and
    tokenize_document tokenizes the extract, so it would also decide which pages the
    document is filed under."""
    intro = "Python is a high-level, general-purpose programming language."
    with patch.object(background, "get_wiki_intro_extracts",
                      return_value={PYTHON.title: intro}):
        documents = background._with_intro_extracts([PYTHON, CORRECTED])

    by_url = {d.url: d for d in documents}
    assert by_url[PYTHON.url].extract == intro
    assert by_url[CORRECTED.url].extract == CORRECTED.extract   # nothing fetched, snippet kept
    assert by_url[PYTHON.url].state == DocumentState.FROM_WIKI
    # The query term has no business travelling with a document filed under its own tokens.
    assert all(document.term is None for document in documents)


# ---------------------------------------------------------------------------
# The background task, end to end
# ---------------------------------------------------------------------------

@pytest.fixture
def wiki_index(tmp_path, redis_client):
    """Point the background task at a temporary index and a fake Redis."""
    TinyIndex.create(item_factory=Document, index_path=str(tmp_path / "index.tinysearch"),
                     num_pages=NUM_PAGES, page_size=PAGE_SIZE)
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()),
                                  redis_client=fakeredis.FakeRedis())
    with override_settings(DATA_PATH=str(tmp_path), INDEX_NAME="index.tinysearch"), \
            patch.object(wiki_cache, "get_redis", return_value=redis_client), \
            patch("mwmbl.indexer.index_batches.get_snapshot_blacklist", return_value=blacklist), \
            patch.object(background, "get_wiki_intro_extracts", return_value={}):
        yield str(tmp_path / "index.tinysearch")


def test_the_task_writes_the_cache_entry_and_the_query_term(wiki_index, redis_client):
    enqueue_wiki_results("python", [PYTHON], redis_client)

    background.index_wiki_results_from_queue.now()

    with TinyIndex(Document, wiki_index, 'r') as index:
        assert {d.url for d in get_cached_wiki_results(index, "python")} == {PYTHON.url}
        # The query term too, because the gate passed.
        assert {d.url for d in index.retrieve("python")} == {PYTHON.url}


def test_the_task_indexes_the_page_under_its_own_tokens(wiki_index, redis_client):
    """The general path files the document under ~57 of its own terms. A term copy landing
    on the same page as one of them costs that one copy - combine_documents dedupes by URL
    across a whole page - so this asserts on the bulk rather than on one chosen token. With
    64 test pages that is nearly every page; with prod's 102.4M it is a rounding error."""
    enqueue_wiki_results("python", [PYTHON], redis_client)

    background.index_wiki_results_from_queue.now()

    tokens = tokenize_document(PYTHON.url, PYTHON.title, PYTHON.extract, PYTHON.score).tokens
    with TinyIndex(Document, wiki_index, 'r') as index:
        found = [token for token in tokens
                 if PYTHON.url in {d.url for d in index.retrieve(token)}]

    assert len(found) >= len(tokens) - 2, f"only {len(found)} of {len(tokens)} terms kept"


def test_the_task_does_nothing_with_an_empty_queue(wiki_index):
    background.index_wiki_results_from_queue.now()

    with TinyIndex(Document, wiki_index, 'r') as index:
        assert index.get_page(0) == []


def test_the_task_only_generally_indexes_a_url_once(wiki_index, redis_client):
    enqueue_wiki_results("python", [PYTHON], redis_client)
    background.index_wiki_results_from_queue.now()

    with patch.object(background.index_batches, "index_documents") as mock_index_documents:
        enqueue_wiki_results("pithon", [PYTHON], redis_client)
        background.index_wiki_results_from_queue.now()

    mock_index_documents.assert_not_called()


@override_settings(WIKI_CACHE_GENERAL_INDEX=False)
def test_the_general_index_kill_switch_leaves_the_cache_working(wiki_index, redis_client):
    enqueue_wiki_results("python", [PYTHON], redis_client)

    background.index_wiki_results_from_queue.now()

    with TinyIndex(Document, wiki_index, 'r') as index:
        assert {d.url for d in get_cached_wiki_results(index, "python")} == {PYTHON.url}
        assert index.retrieve("high-level") == []


def test_a_ranker_that_reads_a_remote_index_does_not_use_the_cache(redis_client):
    """HeuristicAndWikiRanker fetches Wikipedia itself and reads a remote index, where a
    cache lookup is an HTTP round trip that /raw refuses to serve. It must not queue
    anything either - nothing drains that queue on an evaluation run."""
    class RemoteReadingRanker(TrackingRanker):
        cache_wiki_results = False

    ranker = RemoteReadingRanker(cached_page("python", [PYTHON]))
    with patch.object(wiki_cache, "get_redis", return_value=redis_client), \
            patch.object(wiki_cache, "get_cached_wiki_results") as mock_cache_read:
        ranker.search("python", [])

    mock_cache_read.assert_not_called()
    assert redis_client.scard(wiki_cache.WIKI_CACHE_QUEUE_KEY) == 0
    assert ranker.external_search_calls == ["python"]


def test_the_eval_ranker_has_the_cache_turned_off():
    assert HeuristicAndWikiRanker.cache_wiki_results is False
    assert HeuristicRanker.cache_wiki_results is True


def test_an_entry_without_a_term_is_discarded_rather_than_crashing_the_task(redis_client):
    """There is no page to file a termless document under, and the background task would
    raise on get_key_page_index."""
    redis_client.sadd(wiki_cache.WIKI_CACHE_QUEUE_KEY,
                      '{"extract": "", "score": null, "term": null, '
                      '"title": "T", "url": "https://x.test"}')

    assert drain_wiki_queue(10, redis_client) == []
