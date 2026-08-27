"""Changing an index page is read -> merge -> write, and has to be atomic.

_write_page copies ~4 KB into the mmap, and a reader catching it half-written gets a page
that will not decompress. Every read-modify-write goes through TinyIndex.page(), which
holds the page's lock for the whole block, so no caller has to know locking exists.
"""
import errno
import fcntl
import multiprocessing
import os
import random
import string
import struct
from io import UnsupportedOperation
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from mwmbl.indexer.index_batches import index_pages, index_results_against_query
from mwmbl.indexer.purge_blacklisted import purge_documents
from mwmbl.tinysearchengine.indexer import (
    METADATA_SIZE, Document, DocumentState, PageError, TinyIndex)


NUM_PAGES = 64

# The non-blocking form of the lock the index takes, for a child that wants to find out
# whether a page is free rather than wait for it.
F_OFD_SETLK = 37


@pytest.fixture
def index_path():
    with TemporaryDirectory() as temp_dir:
        path = str(Path(temp_dir) / "temp-index.tinysearch")
        with TinyIndex.create(Document, path, num_pages=NUM_PAGES, page_size=4096):
            pass
        yield path


def _try_lock_in_child(index_path: str, page: int, queue):
    """Child process: report whether the page lock is free."""
    from mwmbl.tinysearchengine.indexer import _FLOCK_STRUCT

    with TinyIndex(Document, index_path, 'w') as indexer:
        start = page * indexer.page_size + METADATA_SIZE
        try:
            fcntl.fcntl(indexer.index_file.fileno(), F_OFD_SETLK,
                        struct.pack(_FLOCK_STRUCT, fcntl.F_WRLCK, os.SEEK_SET,
                                    start, indexer.page_size, 0))
            queue.put(True)
        except OSError:
            queue.put(False)


def test_page_holds_the_lock_across_the_read_and_the_write(index_path):
    """The lock must be held for as long as the block runs, and no longer.

    It asserts the exclusion property directly rather than trying to win the race: the
    window is a single memcpy, so a racing test passes with the lock removed and guards
    nothing.
    """
    context = multiprocessing.get_context("fork")
    with TinyIndex(Document, index_path, 'r') as indexer:
        target = indexer.get_key_page_index("zebra")

    def child_can_lock(page_index=target):
        queue = context.Queue()
        child = context.Process(target=_try_lock_in_child, args=(index_path, page_index, queue))
        child.start()
        child.join(timeout=30)
        return queue.get(timeout=5)

    with TinyIndex(Document, index_path, 'w') as indexer:
        with indexer.page(target) as page:
            assert not child_can_lock(), "another process took a lock we were holding"

            # An ordinary POSIX record lock is owned by the *process* and is dropped the
            # moment any fd on the file is closed - and index_results_against_query opens
            # and closes a read handle on every store, so in a threaded worker that would
            # silently release the lock mid-write. OFD locks survive it.
            with TinyIndex(Document, index_path, 'r'):
                pass
            assert not child_can_lock(), \
                "closing an unrelated fd released the lock - this needs an OFD lock"

            assert child_can_lock((target + 1) % NUM_PAGES), "a different page was blocked"
            assert page.documents == []

    assert child_can_lock(), "the lock was not released"


def test_page_writes_nothing_unless_store_is_called(index_path):
    seeded = [Document("Zebra", "https://en.wikipedia.org/wiki/Zebra", "", 3.0, "zebra",
                       state=DocumentState.FROM_WIKI)]
    with TinyIndex(Document, index_path, 'w') as indexer:
        target = indexer.get_key_page_index("zebra")
        indexer.store_in_page(target, seeded)

        with indexer.page(target) as page:
            assert [d.url for d in page.documents] == [seeded[0].url]  # read, then do nothing

    with TinyIndex(Document, index_path, 'r') as indexer:
        assert [d.url for d in indexer.get_page(target)] == [seeded[0].url]


def test_store_reports_how_many_documents_actually_fit(index_path):
    """A page drops the tail that does not fit, which is why this is store() and not an
    assignment to `documents` - what you pass is not necessarily what is kept."""
    # Distinct, incompressible extracts - zstd would squeeze 500 identical ones onto a
    # single page and there would be nothing to truncate.
    filler = random.Random(0)
    many = [Document(f"Zebra {i}", f"https://en.wikipedia.org/wiki/Zebra_{i}",
                     "".join(filler.choices(string.ascii_letters, k=400)),
                     3.0, "zebra", state=DocumentState.FROM_WIKI)
            for i in range(500)]
    with TinyIndex(Document, index_path, 'w') as indexer:
        with indexer.page(indexer.get_key_page_index("zebra")) as page:
            num_stored = page.store(many)

    assert 0 < num_stored < len(many), f"expected truncation, stored {num_stored}"

    with TinyIndex(Document, index_path, 'r') as indexer:
        assert len(indexer.get_page(indexer.get_key_page_index("zebra"))) == num_stored


def test_page_needs_write_mode(index_path):
    with TinyIndex(Document, index_path, 'r') as indexer:
        with pytest.raises(UnsupportedOperation):
            with indexer.page(0):
                pass


def _fail_to_lock(monkeypatch, err: OSError):
    from mwmbl.tinysearchengine import indexer as indexer_module

    monkeypatch.setattr(indexer_module, "_page_locking_supported", True)
    monkeypatch.setattr(indexer_module, "_set_page_lock",
                        lambda *args: (_ for _ in ()).throw(err))
    return indexer_module


def _store_one(index_path: str) -> Document:
    document = Document("Zebra", "https://en.wikipedia.org/wiki/Zebra", "", 3.0, None)
    assert index_results_against_query([document], "zebra", index_path) == 1

    with TinyIndex(Document, index_path, 'r') as indexer:
        assert {d.url for d in indexer.retrieve("zebra")} == {document.url}
    return document


def test_writes_continue_when_page_locking_is_unsupported(index_path, monkeypatch):
    """A filesystem without record locks must not break indexing.

    Every path through index_pages ran with no lock at all before this existed, so an
    environment that cannot lock should degrade to that, not fail.
    """
    indexer_module = _fail_to_lock(monkeypatch, OSError(errno.ENOLCK, "no locks available"))

    _store_one(index_path)

    assert indexer_module._page_locking_supported is False


def test_a_one_off_lock_failure_does_not_disable_locking(index_path, monkeypatch):
    """Only an errno meaning "this environment cannot lock" latches locking off.

    Anything else - a deadlock detection, an interrupted call - is a one-off. Latching on
    it would silently drop the lock for the rest of the process's life on a blip.
    """
    indexer_module = _fail_to_lock(monkeypatch, OSError(errno.EDEADLK, "resource deadlock"))

    _store_one(index_path)

    assert indexer_module._page_locking_supported is True


def _store_repeatedly(index_path: str, term: str, url_prefix: str, count: int):
    """Child-process worker: store documents under `term`, over and over."""
    for i in range(count):
        # The title has to contain the term, or the containment rule stores nothing.
        index_results_against_query(
            [Document(f"Zebra {url_prefix} {i}", f"https://example.com/{url_prefix}/{i}", "",
                      3.0, None)],
            term, index_path)


def test_concurrent_writers_leave_the_page_readable(index_path):
    """Smoke test: four processes hammering one page leave it decodable, not empty."""
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_store_repeatedly,
                               args=(index_path, "zebra", f"w{n}", 30))
               for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0

    with TinyIndex(Document, index_path, 'r') as indexer:
        page = indexer.get_page(indexer.get_key_page_index("zebra"))

    assert page, "the page was left empty"
    assert all(document.title and document.url for document in page)


# ---------------------------------------------------------------------------
# A page that will not decode
# ---------------------------------------------------------------------------

def _doc(i: int, term: str) -> Document:
    return Document(f"Title {i}", f"https://example.com/{i}", "extract", 1.0, term)


def _corrupt(index_path: str, page_index: int):
    """Damage a page that has real content on it.

    It has to be written first: a freshly created page is a few bytes of compressed `[]`
    followed by zero padding, so overwriting the padding with zeros changes nothing.
    """
    with TinyIndex(Document, index_path, 'w') as indexer:
        indexer.store_in_page(page_index, [_doc(i, "seeded") for i in range(5)])

    with open(index_path, "r+b") as index_file:
        index_file.seek(METADATA_SIZE + page_index * 4096 + 16)
        index_file.write(b"\0" * 64)

    with TinyIndex(Document, index_path, 'r') as indexer:
        with pytest.raises(PageError):
            indexer.get_page(page_index)


def test_one_unreadable_page_does_not_abandon_the_rest_of_the_batch(index_path):
    """index_pages runs inside POST /crawler/results and the batch indexer.

    Letting a single page propagate would 500 the submission, or leave a batch unmarked
    and retried forever, and would skip every page after it in the same call.
    """
    _corrupt(index_path, 3)

    counts = index_pages(index_path, {3: [_doc(1, "a")], 4: [_doc(2, "b")], 5: [_doc(3, "c")]})

    with TinyIndex(Document, index_path, 'r') as indexer:
        assert len(indexer.get_page(4)) == 1, "a later page was skipped"
        assert len(indexer.get_page(5)) == 1, "a later page was skipped"
    assert counts["b"] == 1 and counts["c"] == 1


def test_a_corrupt_page_is_reset_by_a_writer_holding_its_lock(index_path):
    """Holding the page's exclusive lock, no other locked writer can be mid-write on it,
    so an unreadable page is real damage rather than a torn read - and nothing else will
    ever repair it, since readers treat it as empty and writers refuse it."""
    _corrupt(index_path, 3)

    index_pages(index_path, {3: [_doc(1, "a")]})

    with TinyIndex(Document, index_path, 'r') as indexer:
        assert [d.url for d in indexer.get_page(3)] == ["https://example.com/1"]


def test_an_unlocked_writer_refuses_a_page_it_could_not_read(index_path, monkeypatch):
    """Without the lock, a page that will not decode is just as likely to be another
    writer's memcpy in progress, and resetting it would store over what they are writing.
    Skip it - and leave it for a writer that can take the lock."""
    _corrupt(index_path, 3)
    _fail_to_lock(monkeypatch, OSError(errno.ENOLCK, "no locks available"))

    index_pages(index_path, {3: [_doc(1, "a")], 4: [_doc(2, "b")]})

    with TinyIndex(Document, index_path, 'r') as indexer:
        assert len(indexer.get_page(4)) == 1, "a later page was skipped"
        with pytest.raises(PageError):
            indexer.get_page(3)  # left alone, not reset and not merged onto


def test_purging_skips_a_page_it_cannot_read(index_path):
    """purge_documents' caller has already drained these documents out of the purge queue,
    so one propagating page would lose the whole batch on every run."""
    blacklisted = Document("Spam", "https://spam.example.com/1", "extract", 1.0, None)
    with TinyIndex(Document, index_path, 'w') as indexer:
        pages = {indexer.get_key_page_index(term)
                 for term in ("spam", "extract", "spam example com")}
    assert len(pages) > 1, "expected the document to be filed under several terms"

    with TinyIndex(Document, index_path, 'w') as indexer:
        for page_index in pages:
            indexer.store_in_page(page_index, [blacklisted])
    _corrupt(index_path, sorted(pages)[0])

    with TinyIndex(Document, index_path, 'w') as indexer:
        removed = purge_documents(indexer, [blacklisted], lambda domain: True)

    assert removed == {"spam.example.com": 1}, "the readable pages were not purged"


def test_damage_that_gets_past_zstd_still_degrades_to_no_results(index_path):
    """retrieve() catches PageError and nothing else, so a page that decompresses into
    something that is not JSON has to arrive as one too - or it 500s a search."""
    from zstandard import ZstdCompressor

    from mwmbl.tinysearchengine.indexer import _pad_to_page_size

    with TinyIndex(Document, index_path, 'r') as indexer:
        page_index = indexer.get_key_page_index("zebra")

    payload = ZstdCompressor().compress(b"\xff\xfe definitely not json")
    with open(index_path, "r+b") as index_file:
        index_file.seek(METADATA_SIZE + page_index * 4096)
        index_file.write(_pad_to_page_size(payload, 4096))

    with TinyIndex(Document, index_path, 'r') as indexer:
        with pytest.raises(PageError):
            indexer.get_page(page_index)
        assert indexer.retrieve("zebra") == []
