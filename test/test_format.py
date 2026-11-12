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

    assert get_result_source(result) == "eusp"
    assert format_result(result, "tokio")["source"] == "eusp"
    assert format_result_v2(result, 1, "tokio")["engine"] == "eusp"


def test_a_staan_result_from_our_index_still_names_staan():
    result = Document("Tokio", "https://tokio.rs/", "An async runtime.", source=DocumentSource.STAAN_INDEXED)

    assert get_result_source(result) == "eusp"


def test_a_document_with_no_source_still_answers_from_its_state():
    result = Document("Python", "https://en.wikipedia.org/wiki/Python", "", state=DocumentState.FROM_WIKI)

    assert get_result_source(result) == "wikipedia"


def test_an_index_document_is_mwmbl():
    result = Document("Something", "https://something.com", "")

    assert get_result_source(result) == "mwmbl"


def _commons_document(file_name: str, source: DocumentSource | None):
    return Document("A file", f"https://commons.wikimedia.org/wiki/File:{file_name}", "A description", source=source)


def test_an_indexed_commons_file_has_an_image_with_its_licence():
    document = _commons_document("Tirzepatide_GLP1R_complex_7rgp.png", DocumentSource.COMMONS_CC_BY_SA_4_0)

    formatted = format_result_v2(document, 1, "tirzepatide")

    assert formatted["engine"] == "commons"
    assert formatted["image"] == {
        "thumbnail_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/20/"
        "Tirzepatide_GLP1R_complex_7rgp.png/330px-Tirzepatide_GLP1R_complex_7rgp.png",
        "license_name": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "page_url": "https://commons.wikimedia.org/wiki/File:Tirzepatide_GLP1R_complex_7rgp.png",
    }


def test_a_licence_with_no_page_has_no_licence_url():
    formatted = format_result_v2(_commons_document("Cat.jpg", DocumentSource.COMMONS_OTHER), 1, "cat")

    assert formatted["image"]["license_name"] == "Other licence"
    assert formatted["image"]["license_url"] is None


def test_a_crawled_commons_page_stays_a_text_result():
    """Nothing vetted a page the crawler found, so it must not be shown as an image."""
    formatted = format_result_v2(_commons_document("Cat.jpg", None), 1, "cat")

    assert formatted["image"] is None
    assert formatted["engine"] == "mwmbl"


def test_a_staan_result_for_a_commons_url_has_no_image():
    formatted = format_result_v2(_commons_document("Cat.jpg", DocumentSource.STAAN), 1, "cat")

    assert formatted["image"] is None


def test_an_ordinary_result_has_a_null_image():
    document = Document("Tokio", "https://tokio.rs/", "An async runtime.", source=DocumentSource.WIKIPEDIA)

    assert format_result_v2(document, 1, "tokio")["image"] is None


def test_an_unsupported_format_is_a_text_result():
    formatted = format_result_v2(_commons_document("Scan.tif", DocumentSource.COMMONS_CC0), 1, "scan")

    assert formatted["image"] is None
    assert formatted["engine"] == "commons"


def test_a_commons_source_on_another_site_has_no_image():
    document = Document("A", "https://example.com/File:Cat.jpg", "x", source=DocumentSource.COMMONS_CC0)

    assert format_result_v2(document, 1, "cat")["image"] is None


def test_formatting_an_image_makes_no_database_query(django_assert_num_queries):
    document = _commons_document("Cat.jpg", DocumentSource.COMMONS_PUBLIC_DOMAIN)

    with django_assert_num_queries(0):
        formatted = format_result_v2(document, 1, "cat")

    assert formatted["image"] is not None
