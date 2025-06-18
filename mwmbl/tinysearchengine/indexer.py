import json
import os
import shutil
import tempfile
from dataclasses import dataclass, asdict, field
from enum import IntEnum
from io import UnsupportedOperation
from logging import getLogger
from mmap import mmap, PROT_READ
from typing import TypeVar, Generic, Callable, List, Optional

import lmdb
import mmh3
from zstandard import ZstdDecompressor, ZstdCompressor, ZstdError

VERSION = 1
METADATA_CONSTANT = b'mwmbl-tiny-search'
METADATA_SIZE = 4096

PAGE_SIZE = 4096


logger = getLogger(__name__)


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


@dataclass
class Document:
    title: str
    url: str
    extract: str
    score: Optional[float] = None
    term: Optional[str] = None
    state: Optional[int] = None

    def __init__(
            self,
            title: str,
            url: str,
            extract: str,
            score: Optional[float] = None,
            term: Optional[str] = None,
            state: Optional[int | DocumentState] = None
    ):
        # Sometimes the title or extract may be None, probably because of user generated content
        # It's not allowed to be None though, or things will break
        self.title = title if title is not None else ''
        self.url = url
        self.extract = extract if extract is not None else ''
        self.score = score
        self.term = term
        self.state = None if state is None else DocumentState(state)

    def as_tuple(self):
        """
        Convert a type to a tuple - values at the end that are None can be truncated.
        """
        values = list(self.__dict__.values())
        if values[-1] is not None:
            values[-1] = values[-1].value

        while values[-1] is None:
            values = values[:-1]
        return tuple(values)


@dataclass
class TokenizedDocument(Document):
    tokens: List[str] = field(default_factory=list)


T = TypeVar('T')


class PageError(Exception):
    pass


@dataclass
class TinyIndexMetadata:
    version: int
    page_size: int
    num_pages: int
    item_factory: str

    def to_bytes(self) -> bytes:
        metadata_bytes = METADATA_CONSTANT + json.dumps(asdict(self)).encode('utf8')
        assert len(metadata_bytes) <= METADATA_SIZE
        return metadata_bytes

    @staticmethod
    def from_bytes(data: bytes):
        constant_length = len(METADATA_CONSTANT)
        metadata_constant = data[:constant_length]
        if metadata_constant != METADATA_CONSTANT:
            raise ValueError("This doesn't seem to be an index file")

        values = json.loads(data[constant_length:].decode('utf8'))
        return TinyIndexMetadata(**values)


# Find the optimal amount of data that fits onto a page
# We do this by leveraging binary search to quickly find the index where:
#     - index+1 cannot fit onto a page
#     - <=index can fit on a page
def _binary_search_fitting_size(compressor: ZstdCompressor, page_size: int, items:list[T], lo:int, hi:int):
    # Base case: our binary search has gone too far
    if lo > hi:
        return -1, None
    # Check the midpoint to see if it will fit onto a page
    mid = (lo+hi)//2
    compressed_data = compressor.compress(json.dumps(items[:mid]).encode('utf8'))
    size = len(compressed_data)
    if size > page_size:
        # We cannot fit this much data into a page
        # Reduce the hi boundary, and try again
        return _binary_search_fitting_size(compressor, page_size, items, lo, mid-1)
    else:
        # We can fit this data into a page, but maybe we can fit more data
        # Try to see if we have a better match
        potential_target, potential_data = _binary_search_fitting_size(compressor, page_size, items, mid+1, hi)
        if potential_target != -1:
            # We found a larger index that can still fit onto a page, so use that
            return potential_target, potential_data
        else:
            # No better match, use our index
            return mid, compressed_data


def _trim_items_to_page(compressor: ZstdCompressor, page_size: int, items:list[T]):
    # Find max number of items that fit on a page
    return _binary_search_fitting_size(compressor, page_size, items, 0, len(items))


def _get_page_data(page_size: int, items: list[T]):
    compressor = ZstdCompressor()
    num_fitting, serialised_data = _trim_items_to_page(compressor, page_size, items)

    compressed_data = compressor.compress(json.dumps(items[:num_fitting]).encode('utf8'))
    assert len(compressed_data) <= page_size, "The data shouldn't get bigger"
    return _pad_to_page_size(compressed_data, page_size)


def _pad_to_page_size(data: bytes, page_size: int):
    page_length = len(data)
    if page_length > page_size:
        raise PageError(f"Data is too big ({page_length}) for page size ({page_size})")
    padding = b'\x00' * (page_size - page_length)
    page_data = data + padding
    return page_data


class TinyIndex(Generic[T]):
    def __init__(self, item_factory: Callable[..., T], index_path, mode='r'):
        if mode not in {'r', 'w'}:
            raise ValueError(f"Mode should be one of 'r' or 'w', got {mode}")

        # Initialize LMDB environment with 1TB map size
        readonly = mode == "r"

        # Check if index_path points to a file (old mmap format) vs directory (LMDB format)
        lmdb_path = str(index_path)
        if os.path.isfile(str(index_path)):
            # Old mmap format detected - check for converted LMDB directory
            lmdb_path = str(index_path) + ".lmdb"
            if not os.path.exists(lmdb_path):
                logger.info(f"Converting old mmap format index at '{index_path}' to LMDB format")
                self._convert_mmap_to_lmdb(str(index_path), item_factory, lmdb_path)
            else:
                logger.info(f"Using existing LMDB index at '{lmdb_path}'")

        self.env = lmdb.open(lmdb_path, readonly=readonly, map_size=1024**4)

        # Read metadata from LMDB
        with self.env.begin() as txn:
            metadata_bytes = txn.get(b'metadata')
            if metadata_bytes is None:
                raise ValueError("No metadata found in index")

        # Remove padding before parsing metadata
        metadata_bytes = metadata_bytes.rstrip(b"\x00")
        metadata = TinyIndexMetadata.from_bytes(metadata_bytes)
        if metadata.item_factory != item_factory.__name__:
            raise ValueError(f"Metadata item factory '{metadata.item_factory}' in the index "
                             f"does not match the passed item factory: '{item_factory.__name__}'")

        self.item_factory = item_factory
        self.index_path = index_path
        self.mode = mode

        self.num_pages = metadata.num_pages
        self.page_size = metadata.page_size
        logger.info(f"Loaded index with {self.num_pages} pages and {self.page_size} page size")
        self.txn = None

    def __enter__(self):
        # Begin transaction - readonly for read mode, write for write mode
        self.txn = self.env.begin(write=(self.mode == 'w'))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.txn:
            if exc_type is None and self.mode == 'w':
                self.txn.commit()
            else:
                self.txn.abort()
        self.env.close()

    def retrieve(self, key: str) -> List[T]:
        index = self.get_key_page_index(key)
        logger.debug(f"Retrieving index {index}")
        page = self.get_page(index)
        return [item for item in page if item.term is None or item.term == key]

    def get_key_page_index(self, key) -> int:
        key_hash = mmh3.hash(key, signed=False)
        return key_hash % self.num_pages

    def get_page(self, i) -> list[T]:
        """
        Get the page at index i, decompress and deserialise it using JSON
        """
        results = self._get_page_tuples(i)
        return [self.item_factory(*item) for item in results]

    def _get_page_tuples(self, i):
        page_key = f'page_{i}'.encode('utf-8')
        page_data = self.txn.get(page_key)
        if page_data is None:
            return []

        # Remove padding if present
        page_data = page_data.rstrip(b'\x00')
        if not page_data:
            return []

        decompressor = ZstdDecompressor()
        try:
            decompressed_data = decompressor.decompress(page_data)
        except ZstdError as e:
            logger.exception(f"Error decompressing page {i}: {e}")
            return []
        return json.loads(decompressed_data.decode('utf8'))

    def store_in_page(self, page_index: int, values: list[T]):
        value_tuples = [value.as_tuple() for value in values]
        self._write_page(value_tuples, page_index)

    def _write_page(self, data, i: int):
        """
        Serialise the data using JSON, compress it and store it at index i.
        If the data is too big, it will store the first items in the list and discard the rest.
        """
        if self.mode != 'w':
            raise UnsupportedOperation("The file is open in read mode, you cannot write")

        page_data = _get_page_data(self.page_size, data)
        logger.debug(f"Got page data of length {len(page_data)}")
        page_key = f'page_{i}'.encode('utf-8')
        self.txn.put(page_key, page_data)

    @staticmethod
    def create(item_factory: Callable[..., T], index_path: str, num_pages: int, page_size: int):
        if os.path.exists(index_path):
            raise FileExistsError(f"Index directory '{index_path}' already exists")

        # Create LMDB environment with 1TB map size
        env = lmdb.open(str(index_path), map_size=1024**4)

        metadata = TinyIndexMetadata(
            VERSION, page_size, num_pages, item_factory.__name__
        )
        metadata_bytes = metadata.to_bytes()
        metadata_padded = _pad_to_page_size(metadata_bytes, METADATA_SIZE)

        page_bytes = _get_page_data(page_size, [])

        with env.begin(write=True) as txn:
            # Store metadata
            txn.put(b'metadata', metadata_padded)
            # Initialize empty pages
            for i in range(num_pages):
                page_key = f'page_{i}'.encode('utf-8')
                txn.put(page_key, page_bytes)

        env.close()
        return TinyIndex(item_factory, index_path=index_path)


    def _convert_mmap_to_lmdb(self, old_index_path: str, item_factory: Callable[..., T], lmdb_path: str):
        """
        Convert an old mmap format index to LMDB format, creating a new directory next to the original file.
        """
        # Read old format metadata and data
        with open(old_index_path, 'rb') as index_file:
            # Read and parse metadata
            metadata_page = index_file.read(METADATA_SIZE)
            metadata_bytes = metadata_page.rstrip(b'\x00')
            metadata = TinyIndexMetadata.from_bytes(metadata_bytes)

            # Verify item factory matches
            if metadata.item_factory != item_factory.__name__:
                raise ValueError(f"Metadata item factory '{metadata.item_factory}' in the old index "
                               f"does not match the passed item factory: '{item_factory.__name__}'")

            # Read all page data using mmap for efficiency
            with mmap(index_file.fileno(), 0, prot=PROT_READ) as old_mmap:
                # Create temporary directory for new LMDB index
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_index_path = os.path.join(temp_dir, "temp_index")

                    # Create new LMDB index
                    temp_env = lmdb.open(str(temp_index_path), map_size=1024**4)

                    # Prepare metadata for new format
                    metadata_padded = _pad_to_page_size(metadata.to_bytes(), METADATA_SIZE)

                    with temp_env.begin(write=True) as txn:
                        # Store metadata
                        txn.put(b"metadata", metadata_padded)

                        # Convert each page
                        for i in range(metadata.num_pages):
                            # Read page from old format
                            start_offset = i * metadata.page_size + METADATA_SIZE
                            end_offset = (i + 1) * metadata.page_size + METADATA_SIZE
                            page_data = old_mmap[start_offset:end_offset]

                            # Store page in new format with same compression/padding
                            page_key = f"page_{i}".encode("utf-8")
                            txn.put(page_key, page_data)

                    temp_env.close()

                    # Move new LMDB directory to target location (next to original file)
                    shutil.move(temp_index_path, lmdb_path)
                    logger.info(f"Successfully converted index to LMDB format at '{lmdb_path}'. Original mmap file preserved at '{old_index_path}'")
