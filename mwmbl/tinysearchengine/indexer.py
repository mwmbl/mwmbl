import json
import os
from dataclasses import dataclass, asdict, field
from enum import IntEnum
from io import UnsupportedOperation
from logging import getLogger
# from mmap import mmap, PROT_READ, PROT_WRITE
from fmmap import mmap, PROT_READ, PROT_WRITE
from time import sleep
from typing import TypeVar, Generic, Callable, List, Optional

import mmh3
from zstandard import ZstdDecompressor, ZstdCompressor, ZstdError

VERSION = 1
METADATA_CONSTANT = b'mwmbl-tiny-search'
METADATA_SIZE = 4096

PAGE_SIZE = 4096


logger = getLogger(__name__)


def astuple(dc):
    """
    Convert a type to a tuple - values at the end that are None can be truncated.
    """
    value = tuple(dc.__dict__.values())
    while value[-1] is None:
        value = value[:-1]
    return value


class DocumentState(IntEnum):
    """
    The state of the document in the index. A value of None indicates an organic search result.
    """
    DELETED = -1
    FROM_USER = 2
    FROM_GOOGLE = 3
    ORGANIC_APPROVED = 7
    FROM_USER_APPROVED = 8
    FROM_GOOGLE_APPROVED = 9


@dataclass
class Document:
    title: str
    url: str
    extract: str
    score: float
    term: Optional[str] = None
    state: Optional[int] = None


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
    checksum_size: int = 0

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


def _get_page_data(compressor: ZstdCompressor, page_size: int, items: list[T]):
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
    def __init__(self, item_factory: Callable[..., T], index_path: str, mode: str = 'r'):
        if mode not in {'r', 'w'}:
            raise ValueError(f"Mode should be one of 'r' or 'w', got {mode}")

        with open(index_path, 'rb') as index_file:
            metadata_page = index_file.read(METADATA_SIZE)

        metadata_bytes = metadata_page.rstrip(b'\x00')
        metadata = TinyIndexMetadata.from_bytes(metadata_bytes)
        if metadata.item_factory != item_factory.__name__:
            raise ValueError(f"Metadata item factory '{metadata.item_factory}' in the index "
                             f"does not match the passed item factory: '{item_factory.__name__}'")

        self.item_factory = item_factory
        self.index_path = index_path
        self.mode = mode

        self.num_pages = metadata.num_pages
        self.page_size = metadata.page_size
        self.checksum_size = metadata.checksum_size
        self.compressor = ZstdCompressor()
        self.decompressor = ZstdDecompressor()
        logger.info(f"Loaded index with {self.num_pages} pages and {self.page_size} page size")
        self.index_file = None
        self.mmap = None

    def __enter__(self):
        self.index_file = open(self.index_path, 'r+b')
        prot = PROT_READ if self.mode == 'r' else PROT_READ | PROT_WRITE
        self.mmap = mmap(self.index_file.fileno(), 0, prot=prot)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.mmap.close()
        self.index_file.close()

    def retrieve(self, key: str) -> List[T]:
        index = self.get_key_page_index(key)
        logger.debug(f"Retrieving index {index}")
        return self.get_page(index)

    def get_key_page_index(self, key) -> int:
        key_hash = mmh3.hash(key, signed=False)
        return key_hash % self.num_pages

    def get_page(self, i) -> list[T]:
        """
        Get the page at index i, decompress and deserialise it using JSON
        """
        logger.info(f"Getting page {i}")
        # results = []
        # for i in range(5):
        #     try:
        #         results = self._get_page_tuples(i)
        #         break
        #     except PageError:
        #         sleep(0.001)
        results = self._get_page_tuples(i)
        return [self.item_factory(*item) for item in results]

    def _get_page_tuples(self, i):
        logger.info(f"Getting page {i}")
        all_data = self.mmap[i * self.page_size + METADATA_SIZE:(i + 1) * self.page_size + METADATA_SIZE]
        page_data = all_data[self.checksum_size:]
        if self.checksum_size > 0:
            checksum = all_data[:self.checksum_size]
            calculated_checksum = mmh3.hash_bytes(page_data)[:self.checksum_size]
            if checksum != calculated_checksum:
                raise PageError(f"Checksums do not match, expected {checksum} but got {calculated_checksum}")
        try:
            decompressed_data = self.decompressor.decompress(page_data)
        except ZstdError as e:
            logger.exception(f"Error decompressing page {i}: {e}")
            return []
        return json.loads(decompressed_data.decode('utf8'))

    def store_in_page(self, page_index: int, values: list[T]):
        value_tuples = [astuple(value) for value in values]
        self._write_page(value_tuples, page_index)

    def _write_page(self, data, i: int):
        """
        Serialise the data using JSON, compress it and store it at index i.
        If the data is too big, it will store the first items in the list and discard the rest.
        """
        if self.mode != 'w':
            raise UnsupportedOperation("The file is open in read mode, you cannot write")

        page_data = _get_page_data(self.compressor, self.page_size - self.checksum_size, data)
        logger.debug(f"Got page data of length {len(page_data)}")
        checksum = mmh3.hash_bytes(page_data)[:self.checksum_size] if self.checksum_size > 0 else b''
        self.mmap[i * self.page_size + METADATA_SIZE:(i+1) * self.page_size + METADATA_SIZE] = checksum + page_data

    @staticmethod
    def create(item_factory: Callable[..., T], index_path: str, num_pages: int, page_size: int, checksum_size: int = 8):
        if os.path.isfile(index_path):
            raise FileExistsError(f"Index file '{index_path}' already exists")

        metadata = TinyIndexMetadata(VERSION, page_size, num_pages, item_factory.__name__, checksum_size)
        metadata_bytes = metadata.to_bytes()
        metadata_padded = _pad_to_page_size(metadata_bytes, METADATA_SIZE)

        compressor = ZstdCompressor()
        page_bytes = _get_page_data(compressor, page_size, [])

        with open(index_path, 'wb') as index_file:
            index_file.write(metadata_padded)
            for i in range(num_pages):
                index_file.write(page_bytes)

        return TinyIndex(item_factory, index_path=index_path)

