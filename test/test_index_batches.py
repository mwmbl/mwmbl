from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import fakeredis
import pytest

from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist
from mwmbl.indexer.index_batches import (
    sort_documents, combine_documents, _merge_user_ids, MAX_USER_IDS,
    index_results_against_query, index_documents,
)
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
# score_terms and the score EMA
# ---------------------------------------------------------------------------

def _make_index(temp_dir, num_pages=64):
    index_path = str(Path(temp_dir) / 'temp-index.tinysearch')
    with TinyIndex.create(Document, index_path, num_pages=num_pages, page_size=4096):
        pass
    return index_path


def _scores_by_term(index_path, terms):
    with TinyIndex(Document, index_path, 'r') as indexer:
        return {term: {d.url: d.score for d in indexer.retrieve(term)} for term in terms}


def test_score_terms_specific_scores_bigrams_only():
    # The score is the rank the source gave the document for the *whole* query, so on a
    # multi-token query only the bigrams may carry it - a bare unigram would claim a
    # ranking that word never earned on its own.
    doc = Document(title="Rust async programming", url="http://a.example/rust",
                   extract="async programming in rust", score=3.0)

    with TemporaryDirectory() as temp_dir:
        index_path = _make_index(temp_dir)
        index_results_against_query([doc], "rust async programming", index_path,
                                    max_term_tokens=2, score_terms="specific")

        scores = _scores_by_term(index_path, ["rust", "async", "programming",
                                              "rust async", "async programming"])

    assert scores["rust"] == {doc.url: None}
    assert scores["async"] == {doc.url: None}
    assert scores["programming"] == {doc.url: None}
    assert scores["rust async"] == {doc.url: 3.0}
    assert scores["async programming"] == {doc.url: 3.0}


def test_score_terms_specific_scores_the_unigram_of_a_one_word_query():
    # A one-token query *is* its unigram, so the score is honestly that term's own.
    doc = Document(title="Kitsas dictionary", url="https://en.wiktionary.org/wiki/kitsas",
                   extract="", score=2.0)

    with TemporaryDirectory() as temp_dir:
        index_path = _make_index(temp_dir)
        index_results_against_query([doc], "kitsas", index_path, score_terms="specific")
        scores = _scores_by_term(index_path, ["kitsas"])

    assert scores["kitsas"] == {doc.url: 2.0}


def test_score_terms_exact_scores_nothing_for_a_long_query():
    # No term equals the whole query once terms are capped at two words, so nothing may
    # carry a score that only the full query earned.
    doc = Document(title="Rust async programming", url="http://a.example/rust",
                   extract="async programming in rust", score=3.0)

    with TemporaryDirectory() as temp_dir:
        index_path = _make_index(temp_dir)
        index_results_against_query([doc], "rust async programming", index_path,
                                    max_term_tokens=2, score_terms="exact")
        scores = _scores_by_term(index_path, ["rust", "rust async", "async programming"])

    assert all(url_scores == {doc.url: None} for url_scores in scores.values())


def test_score_terms_none_is_the_default():
    doc = Document(title="Rust guide", url="http://a.example/rust", extract="rust", score=3.0)

    with TemporaryDirectory() as temp_dir:
        index_path = _make_index(temp_dir)
        index_results_against_query([doc], "rust", index_path)
        assert _scores_by_term(index_path, ["rust"])["rust"] == {doc.url: None}


def test_score_ema_blends_with_the_previous_stored_score():
    url = "http://a.example/rust"
    first = Document(title="Rust guide", url=url, extract="rust", score=3.0,
                     state=DocumentState.FROM_WIKI)
    second = Document(title="Rust guide", url=url, extract="rust", score=1.0,
                      state=DocumentState.FROM_WIKI)

    with TemporaryDirectory() as temp_dir:
        index_path = _make_index(temp_dir)
        index_results_against_query([first], "rust", index_path, score_terms="all",
                                    state=DocumentState.FROM_WIKI, score_ema_alpha=0.5)
        assert _scores_by_term(index_path, ["rust"])["rust"] == {url: 3.0}

        index_results_against_query([second], "rust", index_path, score_terms="all",
                                    state=DocumentState.FROM_WIKI, score_ema_alpha=0.5)
        # 0.5 * 1.0 + 0.5 * 3.0
        assert _scores_by_term(index_path, ["rust"])["rust"] == {url: 2.0}


def test_score_ema_alpha_one_overwrites():
    url = "http://a.example/rust"
    docs = [Document(title="Rust guide", url=url, extract="rust", score=score,
                     state=DocumentState.FROM_WIKI) for score in (3.0, 1.0)]

    with TemporaryDirectory() as temp_dir:
        index_path = _make_index(temp_dir)
        for doc in docs:
            index_results_against_query([doc], "rust", index_path, score_terms="all",
                                        state=DocumentState.FROM_WIKI)
        assert _scores_by_term(index_path, ["rust"])["rust"] == {url: 1.0}


def test_score_ema_ignores_documents_stored_with_another_state():
    # Scores are only comparable within one source: a curated document carries ~1.1e6, a
    # crawled one carries None. Blending across states would mix scales, so a document
    # filed here under a different state is not a previous value.
    url = "http://a.example/rust"
    from_elsewhere = Document(title="Rust guide", url=url, extract="rust", score=100.0,
                              term="rust", state=DocumentState.FROM_GOOGLE)
    incoming = Document(title="Rust guide", url=url, extract="rust", score=3.0,
                        state=DocumentState.FROM_WIKI)

    with TemporaryDirectory() as temp_dir:
        index_path = _make_index(temp_dir)
        with TinyIndex(Document, index_path, 'w') as indexer:
            page_index = indexer.get_key_page_index("rust")
            with indexer.page(page_index) as page:
                page.store([from_elsewhere])

        index_results_against_query([incoming], "rust", index_path, score_terms="all",
                                    state=DocumentState.FROM_WIKI, score_ema_alpha=0.5)

        scores = _scores_by_term(index_path, ["rust"])
    # Unblended: 0.5 * 3.0 + 0.5 * 100.0 would be 51.5.
    assert scores["rust"] == {url: 3.0}


def test_unknown_score_terms_is_rejected():
    doc = Document(title="Rust guide", url="http://a.example/rust", extract="rust")
    with TemporaryDirectory() as temp_dir:
        index_path = _make_index(temp_dir)
        with pytest.raises(ValueError):
            index_results_against_query([doc], "rust", index_path, score_terms="bogus")
