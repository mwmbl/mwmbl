


from mwmbl.format import format_result
from mwmbl.tinysearchengine.indexer import Document


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
