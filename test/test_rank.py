from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from requests.exceptions import RetryError
from urllib3.exceptions import MaxRetryError, ResponseError

from mwmbl.tinysearchengine import rank
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import HeuristicRanker, get_wiki_results


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
    with override_settings(WIKI_CACHE_ENABLED=False), \
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

    with override_settings(WIKI_CACHE_ENABLED=False), \
            patch.object(rank.requests, "Session") as mock_session:
        results = get_wiki_results("query", 5)

    mock_session.assert_not_called()
    assert results == []


def test_is_wiki_rate_limited_detects_429_reason():
    assert rank._is_wiki_rate_limited(_make_retry_error(429))


def test_is_wiki_rate_limited_ignores_non_429_reason():
    assert not rank._is_wiki_rate_limited(_make_retry_error(503))
