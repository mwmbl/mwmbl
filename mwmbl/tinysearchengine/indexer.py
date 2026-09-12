import errno
import fcntl
import json
import os
import struct
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from enum import IntEnum
from io import UnsupportedOperation
from logging import getLogger
from typing import Callable, Generic, List, Optional, TypeVar

import mmh3

from mwmbl.tokenizer import clean_unicode

# Pages are read, packed and written by the Rust extension, which does the positioned
# read, the zstd and the JSON with the GIL released - see mwmbl_rank/src/index.rs.
# PageError is raised from there and imported by the rest of the codebase from here, as
# it always was: a page that will not decode, or data that will not fit on one.
from mwmbl_rank import PageError, pack_index_page, read_index_page, write_index_page

VERSION = 1
METADATA_CONSTANT = b"mwmbl-tiny-search"
METADATA_SIZE = 4096

PAGE_SIZE = 4096

# Where `state` sits in Document.as_tuple(). get_page needs it to blank an unreadable state
# without disturbing the values around it.
STATE_INDEX = 5


logger = getLogger(__name__)


# Open File Description locks (Linux 3.15+). Byte-range like POSIX record locks, but owned
# by the open file description like flock - which is the property that matters here.
# Ordinary fcntl/lockf record locks are owned by the *process* and are dropped as soon as
# any fd on the file is closed, and index_results_against_query opens and closes a read
# handle on the index on every store, so in a threaded worker a plain lockf would be
# released out from under whoever held it. Verified: with lockf, closing an unrelated fd
# lets another process take the "held" lock; with OFD locks it does not.
F_OFD_SETLKW = 38
# struct flock { short l_type; short l_whence; off_t l_start; off_t l_len; pid_t l_pid; }
# Padded to the native 32 bytes so nothing past our buffer is read. l_pid must be 0 for OFD.
# The q's assume a 64-bit off_t, i.e. an LP64 build - true of every platform this runs on.
# On a 32-bit build the buffer would be the wrong shape and fcntl would answer EINVAL,
# which UNSUPPORTED_LOCK_ERRNOS reads as "this kernel cannot lock" and latches locking off.
_FLOCK_STRUCT = "hhqqi4x"

# Errnos that mean this kernel or filesystem cannot do the lock at all, as opposed to a
# one-off failure to take it: an old kernel rejects the F_OFD_SETLKW command with EINVAL,
# and a filesystem with no record-lock support answers ENOLCK/ENOSYS/ENOTSUP. Only these
# latch locking off for the process - see _locked_page.
UNSUPPORTED_LOCK_ERRNOS = frozenset({errno.EINVAL, errno.ENOLCK, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP})

# Set False the first time locking turns out to be unsupported here - see _locked_page.
_page_locking_supported = True


def _set_page_lock(fileno: int, lock_type: int, start: int, length: int):
    fcntl.fcntl(fileno, F_OFD_SETLKW, struct.pack(_FLOCK_STRUCT, lock_type, os.SEEK_SET, start, length, 0))


class DocumentState(IntEnum):
    """
    The state of the document in the index. A value of None indicates an organic search result.
    """

    SYNCED_WITH_MAIN_INDEX = -2
    DELETED = -1
    FROM_USER = 2
    FROM_GOOGLE = 3
    FROM_WIKI = 4
    ORGANIC_APPROVED = 7
    FROM_USER_APPROVED = 8
    FROM_GOOGLE_APPROVED = 9
    FROM_WIKI_APPROVED = 10


CURATED_STATES = {state for state in DocumentState if state.value >= 7}


class DocumentSource(IntEnum):
    """Which external provider a document came from. None means our own crawl.

    Only the external results cache sets this (see mwmbl.indexer.external_cache); documents
    in the search index leave it None, and as_tuple truncates trailing Nones, so they cost
    nothing for it. The values are written to disk, so they are append-only: never reuse a
    number for a different provider, or a stale cache entry becomes a lie about where its
    results came from.
    """

    WIKIPEDIA = 1
    # Reserved, not yet wired up to a provider. The number is here rather than added later
    # because it is the numbering registry: with only one member the source field cannot be
    # told apart from "unset" and the cache's per-source behaviour cannot be tested at all.
    STAAN = 2


@dataclass
class Document:
    title: str
    url: str
    extract: str
    score: Optional[float] = None
    term: Optional[str] = None
    state: Optional[int] = None
    user_ids: Optional[List[int]] = None
    last_crawled: Optional[int] = None
    source: Optional[int] = None

    def __init__(
        self,
        title: str,
        url: str,
        extract: str,
        score: Optional[float] = None,
        term: Optional[str] = None,
        state: Optional[int | DocumentState] = None,
        user_ids: Optional[List[int]] = None,
        last_crawled: Optional[int] = None,
        source: Optional[int | DocumentSource] = None,
    ):
        # Sometimes the title or extract may be None, probably because of user generated content
        # It's not allowed to be None though, or things will break
        self.title = title if title is not None else ""
        self.url = url
        self.extract = extract if extract is not None else ""
        self.score = score
        self.term = term
        self.state = None if state is None else DocumentState(state)
        self.user_ids = user_ids
        self.last_crawled = last_crawled
        self.source = None if source is None else DocumentSource(source)

    def as_tuple(self):
        """
        Convert a type to a tuple - values at the end that are None can be truncated.
        """
        values = [
            self.title,
            self.url,
            self.extract,
            self.score,
            self.term,
            None if self.state is None else self.state.value,
            self.user_ids,
            self.last_crawled,
            None if self.source is None else self.source.value,
        ]
        while values[-1] is None:
            values = values[:-1]
        return tuple(values)


def cleaned_document(document: Document) -> Document:
    """The document with text that cannot be stored on a page taken out of it.

    A lone surrogate is the case that turns up: a title or an extract that a UTF-16 client
    truncated in the middle of an astral character, which arrives as a \\udXXX escape that
    json.loads accepts and no UTF-8 encoder will write. It has to go before the document
    reaches a page, or it costs the whole page the document would be stored on.

    clean_unicode is what tokenize already does to the same text, so the terms a document
    is indexed under do not change - only the text stored alongside them, which until now
    kept a character the reader could not render anyway.
    """
    return replace(document, title=clean_unicode(document.title), extract=clean_unicode(document.extract))


@dataclass
class TokenizedDocument(Document):
    tokens: List[str] = field(default_factory=list)


T = TypeVar("T")


@dataclass
class TinyIndexMetadata:
    version: int
    page_size: int
    num_pages: int
    item_factory: str

    def to_bytes(self) -> bytes:
        metadata_bytes = METADATA_CONSTANT + json.dumps(asdict(self)).encode("utf8")
        assert len(metadata_bytes) <= METADATA_SIZE
        return metadata_bytes

    @staticmethod
    def from_bytes(data: bytes):
        constant_length = len(METADATA_CONSTANT)
        metadata_constant = data[:constant_length]
        if metadata_constant != METADATA_CONSTANT:
            raise ValueError("This doesn't seem to be an index file")

        values = json.loads(data[constant_length:].decode("utf8"))
        return TinyIndexMetadata(**values)


def _pad_to_page_size(data: bytes, page_size: int):
    page_length = len(data)
    if page_length > page_size:
        raise PageError(f"Data is too big ({page_length}) for page size ({page_size})")
    padding = b"\x00" * (page_size - page_length)
    page_data = data + padding
    return page_data


class _OpenPage(Generic[T]):
    """One page of the index, open for a read-modify-write. See TinyIndex.page."""

    def __init__(self, index: "TinyIndex[T]", page_index: int, locked: bool):
        self._index = index
        self._page_index = page_index
        self.reset = False
        try:
            self.documents: List[T] = index.get_page(page_index)
        except PageError:
            if not locked:
                # Without the lock this could equally be another writer's pwrite in
                # progress, and merging onto an empty list would store over whatever it
                # is writing. Refuse; the caller skips this page.
                raise
            # Holding the page's exclusive lock, no other writer that takes the lock can
            # be part-way through it, so this is real damage - a bad block, or a write
            # from before locking existed - rather than a torn read. Nothing else will
            # ever repair it, because every reader treats it as empty and every writer
            # would refuse it, so this write resets the page. That costs whatever was on
            # it, which is already unreadable.
            logger.error("Index page %d is corrupt; resetting it", page_index)
            self.documents = []
            self.reset = True

    def store(self, values: List[T]) -> int:
        """Write these values to the page, returning how many of them actually fit.

        A page holds a fixed number of bytes and the tail that does not fit is dropped, so
        pass them best-first. It is store() rather than an assignment to `documents`
        precisely because what you pass is not necessarily what ends up stored.
        """
        return self._index.store_in_page(self._page_index, values)


class TinyIndex(Generic[T]):
    def __init__(self, item_factory: Callable[..., T], index_path, mode="r"):
        if mode not in {"r", "w"}:
            raise ValueError(f"Mode should be one of 'r' or 'w', got {mode}")

        with open(index_path, "rb") as index_file:
            metadata_page = index_file.read(METADATA_SIZE)

        metadata_bytes = metadata_page.rstrip(b"\x00")
        metadata = TinyIndexMetadata.from_bytes(metadata_bytes)
        if metadata.item_factory != item_factory.__name__:
            raise ValueError(
                f"Metadata item factory '{metadata.item_factory}' in the index "
                f"does not match the passed item factory: '{item_factory.__name__}'"
            )

        self.item_factory = item_factory
        self.index_path = index_path
        self.mode = mode

        self.num_pages = metadata.num_pages
        self.page_size = metadata.page_size
        logger.info(f"Loaded index with {self.num_pages} pages and {self.page_size} page size")
        self.index_file = None

    def __enter__(self):
        # Pages are read and written positionally on this descriptor, in the extension,
        # never through the buffer. The file object is kept rather than a raw descriptor
        # because fileno() feeds the OFD locks in _set_page_lock, and closing it closes the
        # descriptor for us. Mapping the file instead would cost ~800 MiB of unreclaimable
        # page tables per worker for a 390 GiB index, and buy nothing: reading a page
        # copies it out of the mapping either way.
        self.index_file = open(self.index_path, "r+b")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.index_file.close()

    def retrieve(self, key: str) -> List[T]:
        index = self.get_key_page_index(key)
        logger.debug(f"Retrieving index {index}")
        try:
            # Passing the term drops the documents that another term put on this page
            # before any of them is built into a Document.
            return self.get_page(index, term=key)
        except (PageError, OSError):
            # One unreadable page costs this query the results for one term, and the next
            # read of it will very likely succeed. OSError as well as PageError: the read
            # is a syscall, and a device that failed on one page should cost a query that
            # page rather than answer the whole search with a 500. Writers do not swallow
            # either.
            logger.exception("Could not read index page %d", index)
            return []

    def get_key_page_index(self, key) -> int:
        key_hash = mmh3.hash(key, signed=False)
        return key_hash % self.num_pages

    @contextmanager
    def _locked_page(self, i: int):
        """Hold an exclusive lock on page i for a read-modify-write.

        Storing a page is read -> merge -> write, with no atomicity of its own. Two writers
        on the same page lose one writer's documents, which for a cache is tolerable. The
        serious case is a *torn* read: _write_page writes ~4 KB over the page, and a reader
        that catches it half-written gets a ZstdError, which _get_page_tuples turns into an
        empty page. A writer that reads empty then merges and stores wipes out everything
        else on that page - permanent loss in a 400 GB index, not a transient blip. Since
        the wiki index cache writes from the request path, on a large fraction of searches
        and from every gunicorn worker, that stops being hypothetical.

        Private: every read-modify-write on the index goes through page(), so no
        caller has to know this exists. Readers deliberately do not take it - search would
        pay a syscall per retrieve on the hot path, and a torn read costs a reader only one
        query's results for one term, which the next read fixes.

        If the lock cannot be taken, carry on without one. Every indexing path here - batch
        indexing, curation, POST /crawler/results, copy_index, purge - ran with no lock at
        all before this existed, so turning a failure to lock into a hard failure of all of
        them would be a much worse regression than the race it is guarding. An errno that
        means this environment does not support the lock at all latches
        _page_locking_supported off, so the warning is said once rather than per page;
        anything else is treated as a one-off and locking is tried again next time.
        """
        global _page_locking_supported
        if self.mode != "w":
            raise UnsupportedOperation("The file is open in read mode, you cannot lock a page")

        start = i * self.page_size + METADATA_SIZE
        fileno = self.index_file.fileno()
        locked = False
        if _page_locking_supported:
            try:
                _set_page_lock(fileno, fcntl.F_WRLCK, start, self.page_size)
                locked = True
            except OSError as e:
                if e.errno in UNSUPPORTED_LOCK_ERRNOS:
                    _page_locking_supported = False
                    logger.warning(
                        "Index page locking is not available here (%s); index writes will "
                        "race as they did before it was added. Concurrent writers to one "
                        "page can lose documents.",
                        e,
                    )
                else:
                    logger.warning("Could not lock index page %d (%s); writing it unlocked", i, e)
        try:
            yield locked
        finally:
            if locked:
                try:
                    _set_page_lock(fileno, fcntl.F_UNLCK, start, self.page_size)
                except OSError:
                    # Dropped on close anyway; failing here would mask the caller's own error.
                    logger.warning("Could not release the lock on index page %d", i)

    def get_page(self, i, term: Optional[str] = None) -> list[T]:
        """
        Get the page at index i, decompress and deserialise it using JSON.

        Pass `term` to keep only the documents that belong to it - the ones whose term is
        that term, or which have none, exactly as retrieve() needs them.
        """
        results = self._get_page_tuples(i, term)
        items = []
        for item in results:
            try:
                items.append(self.item_factory(*item))
            except ValueError as e:
                logger.error(f"Invalid item in index page {i}, fixing state to None: {e}. Item: {item}")
                # state is at index 5 of Document.as_tuple's layout, and it is not the last
                # element: source follows it on external cache entries. Blank it where it
                # is rather than truncating - truncating would take source off a cache
                # entry, leave the unknown state value in place, and lose the document to
                # the same ValueError on the retry.
                if len(item) <= STATE_INDEX:
                    logger.error(f"Could not recover item in index page {i}, skipping. Item: {item}")
                    continue
                fixed_item = list(item[:STATE_INDEX]) + [None] + list(item[STATE_INDEX + 1 :])
                try:
                    items.append(self.item_factory(*fixed_item))
                except Exception as e2:
                    logger.error(f"Could not recover item in index page {i}, skipping: {e2}. Item: {item}")
        return items

    def _get_page_tuples(self, i, term: Optional[str] = None):
        """The raw tuples on page i. Raises PageError if the page will not decode.

        It deliberately does not fall back to an empty page. A page that fails to
        decompress is either corrupt or caught mid-write, and treating that as "no
        documents here" is how a writer comes to merge onto nothing and store its result
        over everything that was on the page. Readers that would rather have no results
        than an error catch this - see retrieve.
        """
        if not 0 <= i < self.num_pages:
            # Left to the extension, a negative page index is an OverflowError converting
            # the offset and one past the end is a short read; both are this, and callers
            # only handle PageError.
            raise PageError(f"Page {i} is not in an index of {self.num_pages} pages")
        offset = i * self.page_size + METADATA_SIZE
        try:
            return read_index_page(self.index_file.fileno(), offset, self.page_size, term)
        except PageError as e:
            # The extension knows the offset it read, not which page that was.
            raise PageError(f"Could not read page {i}: {e}") from e

    def store_in_page(self, page_index: int, values: list[T]) -> int:
        """Overwrite a page, returning how many of `values` actually fit on it.

        A page holds a fixed number of bytes and the tail that does not fit is dropped, so
        pass the values best-first. To change a page based on what it already holds, use
        page() instead, which does the read and the write under one lock."""
        value_tuples = [value.as_tuple() for value in values]
        return self._write_page(value_tuples, page_index)

    def _write_page(self, data: list, i: int) -> int:
        """
        Serialise the data using JSON, compress it and store it at index i.
        If the data is too big, it will store the first items in the list and discard the
        rest; the number actually stored is returned.
        """
        if self.mode != "w":
            raise UnsupportedOperation("The file is open in read mode, you cannot write")

        offset = i * self.page_size + METADATA_SIZE
        try:
            return write_index_page(self.index_file.fileno(), offset, self.page_size, data)
        except PageError as e:
            raise PageError(f"Could not write page {i}: {e}") from e

    @contextmanager
    def page(self, i: int):
        """Open page i for a read-modify-write, holding its lock throughout.

        Changing a page is read -> merge -> write, and the three have to be atomic
        together. _write_page writes ~4 KB over the page; a reader catching it half-written
        gets a page that will not decompress, and a writer that took that for an empty page
        would store its result over everything else there.

        Locking lives here rather than in the callers because "hold a lock across your read
        and your write" is the kind of rule that gets quietly forgotten. There is one way to
        change a page and it is already correct::

            with index.page(page_index) as page:
                page.store(merge(page.documents))

        Not calling store() writes nothing, which is what a caller that finds nothing to
        change should do.

        Raises PageError if the page will not decode and the lock was not taken, since a
        page that is merely being written by someone else looks exactly the same and must
        not be merged onto. A caller looping over pages should catch that and skip the
        page rather than abandon the rest of them. Holding the lock, an unreadable page is
        real damage instead, and the block runs with `documents` empty and `reset` set -
        see _OpenPage.
        """
        with self._locked_page(i) as locked:
            yield _OpenPage(self, i, locked)

    @staticmethod
    def create(item_factory: Callable[..., T], index_path: str, num_pages: int, page_size: int):
        if os.path.isfile(index_path):
            raise FileExistsError(f"Index file '{index_path}' already exists")

        metadata = TinyIndexMetadata(VERSION, page_size, num_pages, item_factory.__name__)
        metadata_bytes = metadata.to_bytes()
        metadata_padded = _pad_to_page_size(metadata_bytes, METADATA_SIZE)

        page_bytes, _ = pack_index_page(page_size, [])

        with open(index_path, "wb") as index_file:
            index_file.write(metadata_padded)
            for i in range(num_pages):
                index_file.write(page_bytes)

        return TinyIndex(item_factory, index_path=index_path)
