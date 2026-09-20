"""Tests for the Staan search adapter (mwmbl.tinysearchengine.staan).

Staan is shaped like get_wiki_results rather than like a Super Search adapter: it returns
Documents through the external results cache, so these tests cover the same three things the
Wikipedia path is covered for - the parse, the cache round trip, and that a provider being
unavailable costs the caller recall rather than an exception.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from mwmbl.indexer.external_cache import (
    external_cache_path,
    get_cached_external_results,
    store_external_results,
)
from mwmbl.tinysearchengine import staan
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, DocumentSource, TinyIndex
from mwmbl.tinysearchengine.staan import NUM_STAAN_RESULTS, get_staan_results, staan_score

NUM_PAGES = 8

API_RESPONSE = {
    "web": {
        "results": [
            {
                "url": "https://tokio.rs/",
                "title": "Tokio",
                "snippet": "An asynchronous runtime for Rust.",
            },
            {
                "url": "https://docs.rs/tokio",
                "title": "tokio - Rust",
                "description": "API documentation for the tokio crate.",
            },
            {"title": "No URL here", "snippet": "dropped"},
        ]
    }
}


@pytest.fixture
def cache_index(tmp_path):
    """A real, empty external results cache index that the module-level helpers pick up."""
    with override_settings(
        DATA_PATH=str(tmp_path),
        EXTERNAL_CACHE_INDEX_NAME="external-cache.tinysearch",
        EXTERNAL_CACHE_NUM_PAGES=NUM_PAGES,
        EXTERNAL_CACHE_ENABLED=True,
        STAAN_SEARCH_API_KEY="test-key",
    ):
        TinyIndex.create(
            item_factory=Document, index_path=str(external_cache_path()), num_pages=NUM_PAGES, page_size=PAGE_SIZE
        )
        yield str(external_cache_path())


def _patched_staan(response):
    """Stand in for the Staan API, returning `response` (or raising, if it is an error)."""
    mock = patch.object(staan.requests, "get")
    started = mock.start()
    if isinstance(response, Exception):
        started.side_effect = response
    else:
        started.return_value = MagicMock(**{"json.return_value": response})
    return mock, started


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_results_come_back_as_documents_attributed_to_staan(cache_index):
    mock, requested = _patched_staan(API_RESPONSE)
    try:
        results = get_staan_results("rust async")
    finally:
        mock.stop()

    assert [document.url for document in results] == ["https://tokio.rs/", "https://docs.rs/tokio"]
    assert [document.title for document in results] == ["Tokio", "tokio - Rust"]
    assert {document.source for document in results} == {DocumentSource.STAAN}
    assert requested.call_args.kwargs["params"] == {"q": "rust async", "market": "en-us"}
    assert requested.call_args.kwargs["headers"] == {"Authorization": "Bearer test-key"}


def test_the_description_field_stands_in_for_a_missing_snippet(cache_index):
    mock, _ = _patched_staan(API_RESPONSE)
    try:
        results = get_staan_results("rust async")
    finally:
        mock.stop()

    assert results[0].extract == "An asynchronous runtime for Rust."
    assert results[1].extract == "API documentation for the tokio crate."


def test_results_are_scored_by_their_rank(cache_index):
    """`score` becomes the item_score feature, so it is the only channel through which
    Staan's own ranking reaches a model that has no source feature."""
    mock, _ = _patched_staan(API_RESPONSE)
    try:
        results = get_staan_results("rust async")
    finally:
        mock.stop()

    assert [document.score for document in results] == [staan_score(0), staan_score(1)]


def test_a_result_with_no_url_is_dropped(cache_index):
    mock, _ = _patched_staan(API_RESPONSE)
    try:
        results = get_staan_results("rust async")
    finally:
        mock.stop()

    assert all(document.url for document in results)
    assert len(results) == 2


def test_no_more_than_num_staan_results_are_kept(cache_index):
    many = {"web": {"results": [{"url": f"https://example.com/{i}", "title": str(i)} for i in range(50)]}}
    mock, _ = _patched_staan(many)
    try:
        results = get_staan_results("rust async")
    finally:
        mock.stop()

    assert len(results) == NUM_STAAN_RESULTS


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_a_fetch_is_stored_in_the_external_cache(cache_index):
    mock, _ = _patched_staan(API_RESPONSE)
    try:
        get_staan_results("rust async")
    finally:
        mock.stop()

    cached = get_cached_external_results(DocumentSource.STAAN, "rust async")

    assert [document.url for document in cached] == ["https://tokio.rs/", "https://docs.rs/tokio"]


def test_a_cache_hit_does_not_call_the_api(cache_index):
    store_external_results(
        DocumentSource.STAAN,
        "rust async",
        [Document("Tokio", "https://tokio.rs/", "An asynchronous runtime.", source=DocumentSource.STAAN)],
        now=int(time.time()),
    )

    mock, requested = _patched_staan(API_RESPONSE)
    try:
        results = get_staan_results("rust async")
    finally:
        mock.stop()

    assert requested.call_count == 0
    assert [document.url for document in results] == ["https://tokio.rs/"]


def test_a_cache_hit_is_rescored_here_not_by_the_cache(cache_index):
    """The cache stores the rank the provider gave, never a score: turning one into the
    other needs this module's scale."""
    store_external_results(
        DocumentSource.STAAN,
        "rust async",
        [
            Document("Tokio", "https://tokio.rs/", "", source=DocumentSource.STAAN),
            Document("docs.rs", "https://docs.rs/tokio", "", source=DocumentSource.STAAN),
        ],
        now=int(time.time()),
    )

    mock, _ = _patched_staan(API_RESPONSE)
    try:
        results = get_staan_results("rust async")
    finally:
        mock.stop()

    assert [document.score for document in results] == [staan_score(0), staan_score(1)]


def test_a_query_staan_has_nothing_for_is_remembered(cache_index):
    """Otherwise every empty query is re-fetched forever."""
    mock, requested = _patched_staan({"web": {"results": []}})
    try:
        assert get_staan_results("nonsense query") == []
        assert get_staan_results("nonsense query") == []
    finally:
        mock.stop()

    assert requested.call_count == 1


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


def test_a_missing_api_key_costs_recall_not_the_search(cache_index):
    with override_settings(STAAN_SEARCH_API_KEY=""):
        assert get_staan_results("rust async") == []


def test_a_request_failure_costs_recall_not_the_search(cache_index):
    mock, _ = _patched_staan(Exception("boom"))
    try:
        assert get_staan_results("rust async") == []
    finally:
        mock.stop()


def test_a_failure_is_not_remembered_as_an_empty_result(cache_index):
    """A transient failure must never be cached as "Staan has nothing for this"."""
    mock, _ = _patched_staan(Exception("boom"))
    try:
        get_staan_results("rust async")
    finally:
        mock.stop()

    assert get_cached_external_results(DocumentSource.STAAN, "rust async") is None


def test_an_unparseable_response_costs_recall_not_the_search(cache_index):
    mock, _ = _patched_staan({"unexpected": "shape"})
    try:
        assert get_staan_results("rust async") == []
    finally:
        mock.stop()
