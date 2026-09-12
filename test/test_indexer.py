import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from zstandard import ZstdCompressor, ZstdDecompressor

from mwmbl.tinysearchengine.indexer import (
    METADATA_SIZE,
    STATE_INDEX,
    Document,
    DocumentSource,
    DocumentState,
    PageError,
    TinyIndex,
    _pad_to_page_size,
)
from mwmbl_rank import pack_index_page


def test_create_index():
    num_pages = 10
    page_size = 4096

    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / "temp-index.tinysearch"
        with TinyIndex.create(Document, str(index_path), num_pages=num_pages, page_size=page_size) as indexer:
            for i in range(num_pages):
                page = indexer.get_page(i)
                assert page == []


def _documents(count: int) -> list[tuple]:
    return [Document(title=f"text{i}", url=f"text{i}", extract=f"text{i}", score=i).as_tuple() for i in range(count)]


def test_pack_page_fits_everything_that_fits():
    items = _documents(10)

    page, num_stored = pack_index_page(4096, items)

    assert num_stored == len(items)
    assert len(page) == 4096


def test_pack_page_drops_the_tail_that_does_not_fit():
    """A page holds a fixed number of bytes, so the count that comes back is what store()
    reports to its caller and has to be what was actually kept."""
    items = _documents(5000)

    page, num_stored = pack_index_page(4096, items)

    assert 1 < num_stored < len(items)
    assert json.loads(ZstdDecompressor().decompress(page)) == [list(item) for item in items[:num_stored]]


def test_pack_page_raises_when_nothing_fits():
    """Not even an empty page fits in five bytes. Returning a page that is too big would
    have it written over the start of the next one."""
    with pytest.raises(PageError):
        pack_index_page(5, _documents(9))


def test_a_written_page_is_readable_by_python_zstandard():
    """The extension writes the pages, and an older container reading the same index maps
    it and decompresses with python-zstandard. The frames have to stay interchangeable."""
    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / "temp-index.tinysearch")
        TinyIndex.create(Document, index_path, num_pages=4, page_size=4096)
        document = Document("Title", "https://example.com", "Extract", 1.0, "term")

        with TinyIndex(Document, index_path, "w") as index:
            page_index = index.get_key_page_index("term")
            index.store_in_page(page_index, [document])

        with open(index_path, "rb") as index_file:
            page = index_file.read()[METADATA_SIZE + page_index * 4096 :][:4096]

    assert json.loads(ZstdDecompressor().decompress(page)) == [list(document.as_tuple())]


def test_constructing_document_removes_none():
    document = Document(title=None, url="url", extract=None, score=1.0)
    assert document.title == ""
    assert document.extract == ""


def test_as_tuple_with_new_fields():
    doc = Document(title="t", url="u", extract="e", user_ids=[1, 2], last_crawled=1700000000)
    assert doc.as_tuple() == ("t", "u", "e", None, None, None, [1, 2], 1700000000)


def test_as_tuple_strips_trailing_nones():
    doc = Document(title="t", url="u", extract="e")
    assert doc.as_tuple() == ("t", "u", "e")


def test_as_tuple_only_last_crawled_none_strips_it():
    doc = Document(title="t", url="u", extract="e", user_ids=[1])
    assert doc.as_tuple() == ("t", "u", "e", None, None, None, [1])


def test_document_round_trip_with_new_fields():
    doc = Document(title="t", url="u", extract="e", term="q", user_ids=[42], last_crawled=1700000000)
    restored = Document(*doc.as_tuple())
    assert restored.user_ids == [42]
    assert restored.last_crawled == 1700000000


def test_document_backward_compat_old_six_element_tuple():
    old_tuple = ("title", "https://example.com", "extract", None, "term", None)
    doc = Document(*old_tuple)
    assert doc.user_ids is None
    assert doc.last_crawled is None


def test_state_index_matches_the_tuple_layout():
    """get_page blanks the state by position, so the constant has to track as_tuple()."""
    document = Document(
        "Title",
        "https://example.com",
        "Extract",
        1.0,
        "term",
        DocumentState.FROM_WIKI,
        None,
        123,
        DocumentSource.WIKIPEDIA,
    )

    assert document.as_tuple()[STATE_INDEX] == DocumentState.FROM_WIKI.value


def test_a_document_with_an_unreadable_state_keeps_its_other_values():
    """State values are written to disk, so a page can hold one this build does not know.
    Blanking it where it sits is what keeps the document: it is not the last value in the
    tuple - source follows it on external cache entries - so stripping the tail would take
    source off instead, leave the bad state in place, and drop the document on the retry."""
    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / "temp-index.tinysearch")
        TinyIndex.create(Document, index_path, num_pages=2, page_size=4096)
        # title, url, extract, score, term, state, user_ids, last_crawled, source
        item = ["Title", "https://example.com", "Extract", 1.0, "term", 99, None, 123, DocumentSource.WIKIPEDIA.value]
        with TinyIndex(Document, index_path, "w") as index:
            index._write_page([item], 0)

        with TinyIndex(Document, index_path, "r") as index:
            documents = index.get_page(0)

    assert len(documents) == 1
    assert documents[0].state is None
    assert documents[0].source == DocumentSource.WIKIPEDIA
    assert documents[0].last_crawled == 123


def test_a_write_through_another_handle_is_visible_without_reopening():
    """Search and the external cache keep one read handle open for the life of the worker
    while writers come and go on short-lived 'w' handles, so a read has to see a write made
    elsewhere with no flush and no reopen. Positioned reads and writes share the page cache,
    which is what makes that hold."""
    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / "temp-index.tinysearch")
        TinyIndex.create(Document, index_path, num_pages=4, page_size=4096)
        document = Document("Title", "https://example.com", "Extract", 1.0, "term")

        with TinyIndex(Document, index_path, "r") as reader:
            page_index = reader.get_key_page_index("term")
            assert reader.get_page(page_index) == []

            with TinyIndex(Document, index_path, "w") as writer:
                writer.store_in_page(page_index, [document])

            documents = reader.get_page(page_index)

    assert [item.url for item in documents] == ["https://example.com"]


def test_a_page_claiming_more_than_it_could_hold_is_unreadable():
    """The decompressed size comes out of the page's own header, and the buffer for it is
    allocated before a byte is read. A page that says it holds megabytes is damage, and
    reading it would be the allocation that finishes off a worker already short of memory."""
    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / "temp-index.tinysearch")
        TinyIndex.create(Document, index_path, num_pages=4, page_size=4096)
        # Compresses to a few hundred bytes, and its header says it is 4 MiB and a byte -
        # one past the 1024 pages' worth that a page is allowed to claim.
        overlarge = ZstdCompressor().compress(b"a" * (4 * 1024 * 1024 + 1))
        with open(index_path, "r+b") as index_file:
            index_file.seek(METADATA_SIZE)
            index_file.write(_pad_to_page_size(overlarge, 4096))

        with TinyIndex(Document, index_path, "r") as index:
            with pytest.raises(PageError):
                index.get_page(0)


def test_retrieve_keeps_only_the_documents_for_its_term():
    """Terms whose hashes collide share a page. The filtering happens in the extension, so
    the documents another term left there are never built into Document objects at all."""
    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / "temp-index.tinysearch")
        TinyIndex.create(Document, index_path, num_pages=4, page_size=4096)
        wanted = Document("Wanted", "https://wanted.example.com", "Extract", 1.0, "term")
        other = Document("Other", "https://other.example.com", "Extract", 1.0, "another term")
        untermed = Document("Untermed", "https://untermed.example.com", "Extract", 1.0)

        with TinyIndex(Document, index_path, "w") as index:
            index.store_in_page(index.get_key_page_index("term"), [wanted, other, untermed])

        with TinyIndex(Document, index_path, "r") as index:
            retrieved = index.retrieve("term")
            whole_page = index.get_page(index.get_key_page_index("term"))

    assert [item.url for item in retrieved] == [wanted.url, untermed.url]
    assert [item.url for item in whole_page] == [wanted.url, other.url, untermed.url]


def test_a_truncated_page_raises_rather_than_reading_short():
    """A read that stops at the end of the file must not pass for a page. Silently
    decoding whatever came back would let a writer merge onto it and store the result."""
    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / "temp-index.tinysearch")
        TinyIndex.create(Document, index_path, num_pages=4, page_size=4096)
        page_size = 4096
        with open(index_path, "r+b") as index_file:
            index_file.truncate(METADATA_SIZE + 3 * page_size + page_size // 2)

        with TinyIndex(Document, index_path, "r") as index:
            assert index.get_page(2) == []
            with pytest.raises(PageError):
                index.get_page(3)
