import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import fakeredis
import pytest
from django.conf import settings

from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist
from mwmbl.indexer.index_batches import (
    sort_documents, combine_documents, _merge_user_ids, MAX_USER_IDS,
    index_results_against_query, index_documents, index_pages, preprocess_documents,
)
from mwmbl.indexer.wiki_cache import wiki_cache_term
from mwmbl.tinysearchengine.indexer import Document, DocumentState, PAGE_SIZE, TinyIndex


class UrlRanker:
    @staticmethod
    def order_results(terms: list[str], pages: list[Document], is_complete: bool):
        return sorted(pages, key=lambda doc: doc.url)


def test_sort_documents():
    existing_documents = [
        Document(title="title1", url="1", extract="extract1", term="term1"),
        Document(title="title2", url="4", extract="extract2", term="term2"),
        Document(title="title3", url="6", extract="extract3", term="term3"),
        Document(title="title4", url="5", extract="extract4", term="term3"),
    ]

    documents = [
        Document(title="title5", url="2", extract="extract5", term="term1"),
        Document(title="title6", url="3", extract="extract6", term="term2"),

    ]

    # Sort the documents
    sorted_documents = sort_documents(documents, existing_documents, UrlRanker())

    # Existing terms without new documents should not be sorted
    assert sorted_documents == [
        Document(title="title1", url="1", extract="extract1", term="term1"),
        Document(title="title6", url="3", extract="extract6", term="term2"),
        Document(title="title3", url="6", extract="extract3", term="term3"),
        Document(title="title5", url="2", extract="extract5", term="term1"),
        Document(title="title2", url="4", extract="extract2", term="term2"),
        Document(title="title4", url="5", extract="extract4", term="term3"),
    ]


def test_sort_documents_curated_items_first():
    existing_documents = [
        Document(title="title1", url="1", extract="extract1", term="term1"),
        Document(title="title2", url="4", extract="extract2", term="term2"),
        Document(title="title3", url="6", extract="extract3", term="term1", state=DocumentState.ORGANIC_APPROVED),
        Document(title="title4", url="5", extract="extract4", term="term2", state=DocumentState.ORGANIC_APPROVED),
    ]

    documents = [
        Document(title="title5", url="2", extract="extract5", term="term1"),
        Document(title="title6", url="3", extract="extract6", term="term2"),

    ]

    # Sort the documents
    sorted_documents = sort_documents(documents, existing_documents, UrlRanker())

    # Curated items should be first
    assert sorted_documents == [
        Document(title="title3", url="6", extract="extract3", term="term1", state=DocumentState.ORGANIC_APPROVED),
        Document(title="title4", url="5", extract="extract4", term="term2", state=DocumentState.ORGANIC_APPROVED),
        Document(title="title1", url="1", extract="extract1", term="term1"),
        Document(title="title6", url="3", extract="extract6", term="term2"),
        Document(title="title5", url="2", extract="extract5", term="term1"),
        Document(title="title2", url="4", extract="extract2", term="term2"),
    ]


def test_sort_documents_duplicates_keep_synced_state():
    existing_documents = [
        Document(title="title1", url="1", extract="extract1", term="term1", state=DocumentState.SYNCED_WITH_MAIN_INDEX),
    ]

    documents = [
        Document(title="title1", url="1", extract="extract1", term="term1"),
    ]

    # Sort the documents
    combined_documents = combine_documents(documents, existing_documents, mark_synced=False, ranker=UrlRanker())
    print("Combined documents", combined_documents)

    # Curated items should be first
    assert combined_documents == [
        Document(title="title1", url="1", extract="extract1", term="term1", state=DocumentState.SYNCED_WITH_MAIN_INDEX),
    ]


# ---------------------------------------------------------------------------
# index_results_against_query
# ---------------------------------------------------------------------------

def test_index_results_against_query():
    # "rust", "async" and the bigram "rust async" land on distinct pages here,
    # so cross-term URL dedup within a page does not interfere with the asserts.
    num_pages = 64
    a = Document(title="Rust async runtime", url="http://a.example/page", extract="an async runtime")
    b = Document(title="Rust systems guide", url="http://b.example", extract="low level")
    c = Document(title="Async patterns", url="http://c.example", extract="concurrency primitives")
    docs = [a, b, c]

    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / 'temp-index.tinysearch')
        with TinyIndex.create(Document, index_path, num_pages=num_pages, page_size=4096):
            pass

        new_count = index_results_against_query(docs, "rust async", index_path)

        # All three pages are newly added (each matches at least one term).
        assert new_count == 3

        with TinyIndex(Document, index_path, 'r') as indexer:
            rust_urls = {d.url for d in indexer.retrieve("rust")}
            async_urls = {d.url for d in indexer.retrieve("async")}
            bigram_urls = {d.url for d in indexer.retrieve("rust async")}

        # Unigram "rust" matches A and B; "async" matches A and C.
        assert rust_urls == {a.url, b.url}
        assert async_urls == {a.url, c.url}
        # The bigram needs both words present, so only A matches.
        assert bigram_urls == {a.url}

        # Re-indexing the same results adds nothing new.
        assert index_results_against_query(docs, "rust async", index_path) == 0


def test_index_results_against_query_keeps_title_only_documents():
    # Title-only results (empty extract) must still be indexed, matching what
    # Super Search now keeps for display. The term is matched via the URL/title
    # token set, so an empty extract should not exclude the document.
    num_pages = 64
    doc = Document(title="Kitsas dictionary", url="https://en.wiktionary.org/wiki/kitsas", extract="")
    docs = [doc]

    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / 'temp-index.tinysearch')
        with TinyIndex.create(Document, index_path, num_pages=num_pages, page_size=4096):
            pass

        new_count = index_results_against_query(docs, "kitsas", index_path)
        assert new_count == 1

        with TinyIndex(Document, index_path, 'r') as indexer:
            kitsas_urls = {d.url for d in indexer.retrieve("kitsas")}
        assert kitsas_urls == {doc.url}


# ---------------------------------------------------------------------------
# _merge_user_ids
# ---------------------------------------------------------------------------

def test_merge_user_ids_empty_existing():
    assert _merge_user_ids(None, [1]) == [1]


def test_merge_user_ids_basic():
    assert _merge_user_ids([1], [2]) == [1, 2]


def test_merge_user_ids_deduplication_moves_to_end():
    # User already present moves to most-recent position
    assert _merge_user_ids([1, 2], [1]) == [2, 1]


def test_merge_user_ids_capped_at_max():
    assert _merge_user_ids([1, 2], [3]) == [2, 3]


def test_merge_user_ids_both_none():
    assert _merge_user_ids(None, None) is None


# ---------------------------------------------------------------------------
# combine_documents: user_ids and last_crawled merging
# ---------------------------------------------------------------------------

def test_combine_documents_merges_user_ids_for_same_url():
    existing = [Document(title="t", url="http://a.com", extract="e", term="q", user_ids=[1])]
    new_docs = [Document(title="t", url="http://a.com", extract="e", term="q", user_ids=[2])]
    combined = combine_documents(existing, new_docs, mark_synced=False, ranker=UrlRanker())
    assert len(combined) == 1
    assert set(combined[0].user_ids) == {1, 2}


def test_combine_documents_uses_max_last_crawled():
    existing = [Document(title="t", url="http://a.com", extract="e", term="q", last_crawled=1000)]
    new_docs = [Document(title="t", url="http://a.com", extract="e", term="q", last_crawled=2000)]
    combined = combine_documents(existing, new_docs, mark_synced=False, ranker=UrlRanker())
    assert combined[0].last_crawled == 2000


def test_combine_documents_propagates_user_ids_to_winner():
    """When multiple docs share a URL, whichever wins carries the merged user_ids."""
    existing = [Document(title="old", url="http://a.com", extract="e1", term="q", user_ids=[1])]
    new_docs = [Document(title="new", url="http://a.com", extract="e2", term="q", user_ids=[2])]
    combined = combine_documents(existing, new_docs, mark_synced=False, ranker=UrlRanker())
    assert len(combined) == 1
    assert 1 in combined[0].user_ids
    assert 2 in combined[0].user_ids


# ---------------------------------------------------------------------------
# index_documents: blacklist enforcement
# ---------------------------------------------------------------------------
#
# index_documents() is the common choke point for every path that adds content to the
# index (offline batch processing, the trusted-crawler POST /results endpoint, the
# standalone crawl tool) - crawling/link-discovery only stop *new* crawling of a
# blacklisted domain, they don't stop a submitted batch that already contains one of its
# pages, so indexing needs its own blacklist check.

PATCH_TARGET = "mwmbl.indexer.index_batches.get_snapshot_blacklist"


def snapshot_blacklist(domains: set[str]) -> SnapshotBlacklist:
    """A blacklist backed by the given domains alone.

    index_documents() reads the published snapshot rather than constructing the remote
    providers, so the double it gets has to be a SnapshotBlacklist. Supplying the domains
    as its built-in rules keeps these tests off the snapshot machinery, which
    test_blacklist_snapshot.py covers.
    """
    return SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(domains),
                             redis_client=fakeredis.FakeRedis())


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "test.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=10, page_size=PAGE_SIZE)
    return str(path)


def _all_urls(index_path, num_pages=10):
    urls = []
    with TinyIndex(item_factory=Document, index_path=index_path, mode="r") as index:
        for page_index in range(num_pages):
            urls.extend(doc.url for doc in index.get_page(page_index))
    return urls


def test_index_documents_skips_blacklisted_domain(index_path):
    documents = [
        Document(title="Bad", url="https://fineartteens.com/x", extract="teen gallery"),
        Document(title="Good", url="https://example.com/y", extract="a good page"),
    ]

    with patch(PATCH_TARGET, return_value=snapshot_blacklist({"fineartteens.com"})):
        index_documents(documents, index_path)

    urls = _all_urls(index_path)
    assert "https://fineartteens.com/x" not in urls
    assert "https://example.com/y" in urls


def test_index_documents_keeps_everything_when_nothing_blacklisted(index_path):
    documents = [Document(title="Good", url="https://example.com/y", extract="a good page")]

    with patch(PATCH_TARGET, return_value=snapshot_blacklist(set())):
        index_documents(documents, index_path)

    assert "https://example.com/y" in _all_urls(index_path)


# ---------------------------------------------------------------------------
# Wikipedia cache entries on a page being rewritten
# ---------------------------------------------------------------------------

def test_preprocess_documents_carries_the_state_onto_the_term_copies(index_path):
    """A Wikipedia page indexed the standard way has to keep reporting source
    `wikipedia`, so the per-token copies need its state."""
    document = Document(title="Bananas", url="https://example.com/bananas", extract="fruit",
                        state=DocumentState.FROM_WIKI)

    page_documents = preprocess_documents([document], index_path)

    copies = [copy for copies in page_documents.values() for copy in copies]
    assert copies
    assert {copy.state for copy in copies} == {DocumentState.FROM_WIKI}


def test_index_pages_drops_expired_cache_entries_from_a_page_it_rewrites(index_path):
    """Pages truncate their tail, so cache entries would otherwise pile up on whatever page
    they landed on and never leave. index_pages is where a page gets rewritten anyway."""
    cache_term = wiki_cache_term("bananas")
    stale = Document(title="Stale", url="https://stale.test", extract="", term=cache_term,
                     state=DocumentState.FROM_WIKI,
                     last_crawled=int(time.time()) - settings.WIKI_CACHE_TTL_SECONDS - 1)

    with TinyIndex(Document, index_path, 'r') as index:
        page = index.get_key_page_index(cache_term)
    index_pages(index_path, {page: [stale]})
    with TinyIndex(Document, index_path, 'r') as index:
        assert [d.url for d in index.get_page(page)] == [stale.url]

    # Any later write to that page clears it out.
    index_pages(index_path, {page: [Document(title="New", url="https://new.test",
                                             extract="", term=cache_term,
                                             state=DocumentState.FROM_WIKI,
                                             last_crawled=int(time.time()))]})

    with TinyIndex(Document, index_path, 'r') as index:
        assert [d.url for d in index.get_page(page)] == ["https://new.test"]
