import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import override_settings

from mwmbl.tinysearchengine import rank
from mwmbl.tinysearchengine.indexer import Document, DocumentSource
from mwmbl.tinysearchengine.ltr_rank import LTRRanker
from mwmbl.tinysearchengine.rank import (
    NUM_WIKI_RESULTS,
    HeuristicAndWikiRanker,
    HeuristicRanker,
    get_wiki_results,
    wiki_score,
)


def test_order_result():
    doc1 = Document(title="title2", url="https://something.com", extract="extract2", score=2.0)
    doc2 = Document(title="title3", url="https://something.com", extract="extract3", score=3.0)
    doc3 = Document(title="Bananas and apples", url="https://something.com", extract="extract1", score=1.0)

    documents = [doc1, doc2, doc3]

    ranker = HeuristicRanker(None, None)

    # Sort the documents
    ordered_results = ranker.order_results(["bananas"], documents, True)

    assert ordered_results[0].title == "Bananas and apples"


class _TrackingRanker(HeuristicRanker):
    """A ranker whose external_search (e.g. live Wikipedia lookups) is trackable."""

    def __init__(self):
        tiny_index = MagicMock()
        tiny_index.retrieve.return_value = []
        completer = MagicMock()
        completer.complete.return_value = []
        super().__init__(tiny_index, completer)
        self.external_search_calls = []

    def external_search(self, q):
        self.external_search_calls.append(q)
        return []


def test_complete_never_triggers_external_search():
    # Autocomplete fires on every keystroke, so it must never call external_search
    # (e.g. a live Wikipedia lookup) - see fix-wiki-overuse.
    ranker = _TrackingRanker()
    ranker.complete("some quer")
    assert ranker.external_search_calls == []


def test_search_still_triggers_external_search():
    ranker = _TrackingRanker()
    ranker.search("some query", [])
    assert ranker.external_search_calls == ["some query"]


def test_search_retrieved_ranks_like_search_without_external_search():
    """Combined Search retrieves from the index while Staan is in flight, then ranks the two
    together. That split must rank exactly as a single search() call would."""
    index_pages = [
        Document("Bananas", "https://a.com/", "Bananas and apples", 1.0),
        Document("Apples", "https://b.com/", "Only apples", 2.0),
        Document("Bananas again", "https://a.com/", "A repeat of the first URL", 0.5),
    ]
    additional = [Document("Banana bread", "https://c.com/", "Bananas, baked", 3.0)]
    ranker = _TrackingRanker()
    ranker.tiny_index.retrieve.return_value = index_pages

    retrieved = ranker.search_retrieved(ranker.retrieve("bananas "), additional)
    searched = ranker.search("bananas ", additional, use_external_search=False)

    assert [d.url for d in retrieved] == [d.url for d in searched]
    assert sorted(d.url for d in retrieved) == ["https://a.com/", "https://c.com/"]
    assert ranker.external_search_calls == []


def test_a_staan_copy_from_the_index_keeps_its_source():
    """Read off a term's page rather than the curation term's, as it is for any query other
    than the one it was stored under, it still goes out labelled as Staan's."""
    staan_copy = Document(
        "Tokio async runtime",
        "https://tokio.rs/",
        "An async runtime for Rust.",
        1.0,
        term="tokio",
        source=DocumentSource.STAAN_INDEXED,
    )
    ranker = _TrackingRanker()
    ranker.tiny_index.retrieve.side_effect = lambda term: [staan_copy] if term == "tokio" else []

    (result,) = ranker.search("async tokio ", [], use_external_search=False)

    assert result.source == DocumentSource.STAAN_INDEXED


@pytest.fixture(autouse=True)
def _reset_wiki_circuit():
    cache.delete(rank.WIKI_CIRCUIT_KEY)
    yield
    cache.delete(rank.WIKI_CIRCUIT_KEY)


def _wiki_response(status_code: int, headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = {"query": {"search": []}}
    return response


def _get_wiki_results_with_session(session, **kwargs):
    # The cache is off in these tests: they are about what the fetch does when Wikipedia
    # misbehaves, and a hit would return before the fetch ever ran.
    with override_settings(EXTERNAL_CACHE_ENABLED=False), patch.object(rank.requests, "Session") as mock_session:
        mock_session.return_value.__enter__.return_value = session
        return get_wiki_results("query", 5, **kwargs)


def test_a_wiki_429_trips_the_circuit_for_as_long_as_retry_after_says():
    session = MagicMock()
    session.get.return_value = _wiki_response(429, {"Retry-After": "43"})

    with patch.object(rank.cache, "set") as cache_set:
        results = _get_wiki_results_with_session(session)

    assert results == []
    cache_set.assert_called_once_with(rank.WIKI_CIRCUIT_KEY, 1, timeout=43)


def test_live_search_does_not_retry_or_wait_on_a_wiki_429():
    """Regression: retrying with Retry-After held a search for over a minute, and with it
    every request queued on the same worker."""
    session = MagicMock()
    session.get.return_value = _wiki_response(429, {"Retry-After": "43"})

    _get_wiki_results_with_session(session)

    session.mount.assert_not_called()
    assert session.get.call_count == 1
    assert rank._wiki_circuit_open()


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("11", 11),
        ("0", 1),
        ("86400", rank.WIKI_CIRCUIT_MAX_SECONDS),
        ("Wed, 21 Oct 2026 07:28:00 GMT", rank.WIKI_CIRCUIT_DEFAULT_SECONDS),
        (None, rank.WIKI_CIRCUIT_DEFAULT_SECONDS),
    ],
)
def test_the_cooldown_comes_from_retry_after_within_bounds(retry_after, expected):
    headers = {} if retry_after is None else {"Retry-After": retry_after}

    assert rank._wiki_cooldown_seconds(_wiki_response(429, headers)) == expected


def test_a_wiki_error_other_than_429_does_not_trip_the_circuit():
    session = MagicMock()
    session.get.side_effect = ConnectionError("reset")

    results = _get_wiki_results_with_session(session)

    assert results == []
    assert not rank._wiki_circuit_open()


def test_open_wiki_circuit_short_circuits_without_calling_wikipedia():
    rank._trip_wiki_circuit(60)

    with override_settings(EXTERNAL_CACHE_ENABLED=False), patch.object(rank.requests, "Session") as mock_session:
        results = get_wiki_results("query", 5)

    mock_session.assert_not_called()
    assert results == []


def test_waiting_out_rate_limits_retries_and_ignores_the_circuit():
    """Evaluation runs would rather wait than lose a query's wiki results."""
    rank._trip_wiki_circuit(60)
    session = MagicMock()
    session.get.return_value = _wiki_response(200)

    _get_wiki_results_with_session(session, wait_out_rate_limits=True)

    session.mount.assert_called_once()
    assert session.get.call_count == 1


# ---------------------------------------------------------------------------
# Keeping the wiki results the model is served the ones it was trained on
# ---------------------------------------------------------------------------


def test_the_wiki_score_depends_on_rank_alone():
    """`score` is a feature of the LTR model. It used to be max_wiki_results + 1 - i, so a
    caller asking for a different number of results moved a feature value and with it the
    ranking - training built its pools with five, serving asked for three."""
    assert wiki_score(0) == 6.0
    assert [wiki_score(rank) for rank in range(3)] == [6.0, 5.0, 4.0]


def test_every_ranker_takes_its_wiki_result_count_from_one_place():
    """The count the dataset is built with and the count production ranks with have to be
    the same, and what keeps them the same is that there is only one number."""
    assert LTRRanker.__init__.__defaults__[-1] == NUM_WIKI_RESULTS
    assert HeuristicAndWikiRanker.__init__.__defaults__[-1] == NUM_WIKI_RESULTS
    assert get_wiki_results.__defaults__[-1] == NUM_WIKI_RESULTS


def test_no_call_site_passes_its_own_wiki_result_count():
    """How they came apart in the first place: mwmbl/search_setup.py passed 3 while
    mwmbl/rankeval/ltr/dataset.py trained on 5, and nothing connected the two literals. A
    call site that needs a different number needs a retrained model, so it should have to
    change NUM_WIKI_RESULTS to get one."""
    root = Path(__file__).parent.parent
    literal_count = re.compile(r"(?:num|max)_wiki_results(?:\s*:\s*int)?\s*=\s*\d")
    offenders = [
        f"{path.relative_to(root)}:{lineno}: {line.strip()}"
        for path in sorted([*root.glob("mwmbl/**/*.py"), *root.glob("scripts/**/*.py")])
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        # Code only: the comments around here talk about the counts they are explaining.
        if literal_count.search(line.split("#", 1)[0])
    ]

    assert offenders == []
