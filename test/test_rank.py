import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from requests.exceptions import RetryError
from urllib3.exceptions import MaxRetryError, ResponseError

from mwmbl.tinysearchengine import rank
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.ltr_rank import LTRRanker
from mwmbl.tinysearchengine.rank import (NUM_WIKI_RESULTS, HeuristicAndWikiRanker,
                                         HeuristicRanker, get_wiki_results, wiki_score)


def test_order_result():
    doc1 = Document(title='title2', url='https://something.com', extract='extract2', score=2.0)
    doc2 = Document(title='title3', url='https://something.com', extract='extract3', score=3.0)
    doc3 = Document(title='Bananas and apples', url='https://something.com', extract='extract1', score=1.0)

    documents = [doc1, doc2, doc3]

    ranker = HeuristicRanker(None, None)

    # Sort the documents
    ordered_results = ranker.order_results(["bananas"], documents, True)

    assert ordered_results[0].title == 'Bananas and apples'


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


def _make_retry_error(status_code: int) -> RetryError:
    reason = ResponseError(f"too many {status_code} error responses")
    max_retry_error = MaxRetryError(
        pool=None, url="https://en.wikipedia.org/w/api.php?srsearch=secret-query", reason=reason
    )
    return RetryError(max_retry_error)


@pytest.fixture(autouse=True)
def _reset_wiki_circuit():
    rank._wiki_blocked_until = 0.0
    yield
    rank._wiki_blocked_until = 0.0


def _get_wiki_results_with_session(session):
    # The cache is off in these tests: they are about what the fetch does when Wikipedia
    # misbehaves, and a hit would return before the fetch ever ran.
    with override_settings(EXTERNAL_CACHE_ENABLED=False), \
            patch.object(rank.requests, "Session") as mock_session:
        mock_session.return_value.__enter__.return_value = session
        return get_wiki_results("query", 5)


def test_wiki_429_retry_error_trips_circuit_breaker():
    session = MagicMock()
    session.get.side_effect = _make_retry_error(429)

    results = _get_wiki_results_with_session(session)

    assert results == []
    assert rank._wiki_circuit_open()


def test_wiki_non_429_retry_error_does_not_trip_circuit_breaker():
    session = MagicMock()
    session.get.side_effect = _make_retry_error(503)

    results = _get_wiki_results_with_session(session)

    assert results == []
    assert not rank._wiki_circuit_open()


def test_wiki_query_text_containing_429_is_not_mistaken_for_rate_limit():
    # Regression: matching on str(e) (which embeds the request URL/query) meant a
    # query merely containing "429" could be misread as a 429 rate limit.
    session = MagicMock()
    session.get.side_effect = _make_retry_error(503)

    _get_wiki_results_with_session(session)

    assert not rank._wiki_circuit_open()


def test_open_wiki_circuit_short_circuits_without_calling_wikipedia():
    rank._trip_wiki_circuit()

    with override_settings(EXTERNAL_CACHE_ENABLED=False), \
            patch.object(rank.requests, "Session") as mock_session:
        results = get_wiki_results("query", 5)

    mock_session.assert_not_called()
    assert results == []


def test_is_wiki_rate_limited_detects_429_reason():
    assert rank._is_wiki_rate_limited(_make_retry_error(429))


def test_is_wiki_rate_limited_ignores_non_429_reason():
    assert not rank._is_wiki_rate_limited(_make_retry_error(503))


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
