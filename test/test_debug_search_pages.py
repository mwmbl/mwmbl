import pytest
from django.core.management import call_command
from django.test import override_settings

from mwmbl.indexer.index_batches import index_documents
from mwmbl.tinysearchengine.indexer import Document, METADATA_SIZE, PAGE_SIZE, TinyIndex


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "test.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=10, page_size=PAGE_SIZE)
    return str(path)


@pytest.mark.django_db
def test_reports_well_formed_pages(index_path, capsys):
    index_documents([Document(title="Example", url="https://example.com/x", extract="an example")], index_path)

    with override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("debug_search_pages", "--query", "example")

    out = capsys.readouterr().out
    assert "All pages this query touches are well-formed" in out
    assert "SUSPECT PAGES" not in out


@pytest.mark.django_db
def test_flags_page_that_is_not_a_valid_frame(index_path, capsys):
    """A page of garbage must be reported as suspect rather than decompressed - the whole
    point is to identify the page that would crash the worker without reproducing the crash."""
    index_documents([Document(title="Example", url="https://example.com/x", extract="an example")], index_path)

    with TinyIndex(item_factory=Document, index_path=index_path, mode="w") as index:
        page = index.get_key_page_index("example")
        start = page * index.page_size + METADATA_SIZE
        index.mmap[start:start + index.page_size] = bytes([7]) * index.page_size

    with override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("debug_search_pages", "--query", "example")

    out = capsys.readouterr().out
    assert "SUSPECT PAGES" in out
    assert "NOT A VALID ZSTD FRAME" in out
