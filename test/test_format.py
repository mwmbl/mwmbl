from mwmbl.format import DOCUMENT_PROVIDERS, format_result, format_result_v2, get_result_source
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, DocumentState


def test_format_result():
    result = Document("Something Bananas", "https://something.com", "Insist in Bananas")
    formatted = format_result(result, "in bananas")
    assert formatted["title"] == [
        {"value": "Something ", "is_bold": False},
        {"value": "Bananas", "is_bold": True},
    ]

    assert formatted["extract"] == [
        {"value": "Insist in ", "is_bold": False},
        {"value": "Bananas", "is_bold": True},
    ]


def test_every_document_source_can_be_named():
    """DocumentSource is append-only and the wire name of each member is looked up directly,
    so a new provider must be added to DOCUMENT_PROVIDERS at the same time - otherwise the
    first result it returns raises inside a response."""
    assert set(DOCUMENT_PROVIDERS) == set(DocumentSource)


def test_an_external_provider_names_itself():
    """Without this a Staan result reports as "mwmbl": it has no DocumentState of its own,
    because it never enters the index."""
    result = Document("Tokio", "https://tokio.rs/", "An async runtime.", source=DocumentSource.STAAN)

    assert get_result_source(result) == "staan"
    assert format_result(result, "tokio")["source"] == "staan"
    assert format_result_v2(result, 1, "tokio")["engine"] == "staan"


def test_a_document_with_no_source_still_answers_from_its_state():
    result = Document("Python", "https://en.wikipedia.org/wiki/Python", "", state=DocumentState.FROM_WIKI)

    assert get_result_source(result) == "wikipedia"


def test_an_index_document_is_mwmbl():
    result = Document("Something", "https://something.com", "")

    assert get_result_source(result) == "mwmbl"
