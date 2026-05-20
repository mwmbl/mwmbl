from mwmbl.indexer.index_batches import sort_documents, combine_documents, _merge_user_ids, MAX_USER_IDS
from mwmbl.tinysearchengine.indexer import Document, DocumentState


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
