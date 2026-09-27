"""GET /api/v1/search/page/{n}: the raw, decompressed contents of one index page."""

import pytest
from django.test import Client

from mwmbl.search_setup import tiny_index
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

PAGE_DOCUMENTS = [
    Document(title="Python", url="https://python.org/", extract="The Python language", score=1.5, term="python"),
    Document(title="Rust", url="https://rust-lang.org/", extract="The Rust language", score=0.5, term="rust"),
]


@pytest.fixture
def stored_page():
    """Put known documents on page 0, putting back whatever was there afterwards."""
    page_index = 0
    with TinyIndex(Document, tiny_index.index_path, mode="w") as writer:
        original = writer.get_page(page_index)
        writer.store_in_page(page_index, PAGE_DOCUMENTS)
        yield page_index
        writer.store_in_page(page_index, original)


def test_page_returns_stored_tuples(stored_page):
    response = Client().get(f"/api/v1/search/page/{stored_page}")

    assert response.status_code == 200
    expected_tuples = [list(document.as_tuple()) for document in PAGE_DOCUMENTS]
    assert response.json() == expected_tuples


@pytest.mark.parametrize("page_index", [-1, tiny_index.num_pages])
def test_page_out_of_range_is_404(page_index):
    response = Client().get(f"/api/v1/search/page/{page_index}")

    assert response.status_code == 404
