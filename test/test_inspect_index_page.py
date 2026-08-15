import json

import pytest
from django.core.management import call_command
from django.test import override_settings
from zstandard import ZstdCompressor

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
    assert "Raw item count (parsed directly from JSON): 1" in out
    assert "get_page(" in out and "returns: 1 items" in out


@pytest.mark.django_db
def test_detects_item_silently_dropped_by_get_page(index_path, capsys):
    """Simulates a real failure mode: an item whose stored state value isn't a valid
    DocumentState, with another field (user_ids) stored after it. get_page()'s recovery
    only strips the *last* tuple element and retries once - since state isn't the last
    element here, the retry fails too, and the item is dropped from get_page()'s result
    with only a log line, not an exception - invisible to any caller, including the purge
    tool, that only ever calls get_page()."""
    good_item = ["Good", "https://example.com/x", "extract", None, "term"]
    # title, url, extract, score, term, state (invalid), user_ids - state isn't last, so
    # get_page()'s "strip the last field and retry" recovery can't fix it.
    bad_item = ["Bad", "https://fineartteens.com/y", "extract", None, "term", False, [1]]
    items = [good_item, bad_item]

    with TinyIndex(item_factory=Document, index_path=index_path, mode="w") as index:
        page = index.get_key_page_index("term")
        compressed = ZstdCompressor().compress(json.dumps(items).encode("utf8"))
        padded = compressed + b"\x00" * (index.page_size - len(compressed))
        start = page * index.page_size + METADATA_SIZE
        index.mmap[start:start + index.page_size] = padded

    with override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("inspect_index_page", "--page", str(page))

    out = capsys.readouterr().out
    assert "Raw item count (parsed directly from JSON): 2" in out
    assert "fineartteens.com" in out  # visible in the raw JSON domain breakdown
    assert "FAILED Document(*item) construction" in out
    assert "returns: 1 items (vs 2 in the raw JSON)" in out


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
