from mwmbl.indexer.index_batches import sort_documents
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
