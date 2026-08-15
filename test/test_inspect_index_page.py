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
def test_reports_well_formed_page(index_path, capsys):
    documents = [Document(title="Example", url="https://example.com/x", extract="an example page")]
    index_documents(documents, index_path)

    with override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("inspect_index_page", "--term", "example")

    out = capsys.readouterr().out
    assert "DECOMPRESSION FAILED" not in out
    assert "example.com" in out
    assert "Item count: 1" in out


@pytest.mark.django_db
def test_detects_corrupted_page(index_path, capsys):
    # Write garbage directly into a page's byte range, bypassing the normal write path -
    # simulating a torn/corrupted write.
    with TinyIndex(item_factory=Document, index_path=index_path, mode="w") as index:
        garbage = bytes([1, 2, 3, 4] * (index.page_size // 4))
        start = 0 * index.page_size + METADATA_SIZE
        index.mmap[start:start + index.page_size] = garbage

    with override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("inspect_index_page", "--page", "0")

    out = capsys.readouterr().out
    assert "DECOMPRESSION FAILED" in out


def test_requires_term_or_page():
    with pytest.raises(ValueError):
        call_command("inspect_index_page")
