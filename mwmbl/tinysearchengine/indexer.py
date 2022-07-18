import json
import os
from dataclasses import astuple, dataclass, asdict
from io import UnsupportedOperation
from logging import getLogger
from mmap import mmap, PROT_READ, PROT_WRITE
from typing import TypeVar, Generic, Callable, List

import mmh3
from zstandard import ZstdDecompressor, ZstdCompressor, ZstdError

VERSION = 1
METADATA_CONSTANT = b'mwmbl-tiny-search'
METADATA_SIZE = 4096

NUM_PAGES = 5_120_000
PAGE_SIZE = 4096


logger = getLogger(__name__)


@dataclass
class Document:
    title: str
    url: str
    extract: str
    score: float


@dataclass
class TokenizedDocument(Document):
    tokens: List[str]


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


def _get_page_data(compressor, page_size, data):
    serialised_data = json.dumps(data)
    compressed_data = compressor.compress(serialised_data.encode('utf8'))
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
        self.compressor = ZstdCompressor()
        self.decompressor = ZstdDecompressor()
        logger.info(f"Loaded index with {self.num_pages} pages and {self.page_size} page size")
        self.index_file = None
        self.mmap = None

    def __enter__(self):
        self.index_file = open(self.index_path, 'r+b')
        prot = PROT_READ if self.mode == 'r' else PROT_READ | PROT_WRITE
        self.mmap = mmap(self.index_file.fileno(), 0, offset=METADATA_SIZE, prot=prot)
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
        results = self._get_page_tuples(i)
        return [self.item_factory(*item) for item in results]

    def _get_page_tuples(self, i):
        page_data = self.mmap[i * self.page_size:(i + 1) * self.page_size]
        try:
            decompressed_data = self.decompressor.decompress(page_data)
        except ZstdError:
            logger.exception(f"Error decompressing page data, content: {page_data}")
            return []
        # logger.debug(f"Decompressed data: {decompressed_data}")
        return json.loads(decompressed_data.decode('utf8'))

    def index(self, key: str, value: T):
        assert type(value) == self.item_factory, f"Can only index the specified type" \
                                              f" ({self.item_factory.__name__})"
        page_index = self.get_key_page_index(key)
        try:
            self.add_to_page(page_index, [value])
        except PageError:
            pass

    def add_to_page(self, page_index: int, values: list[T]):
        current_page = self._get_page_tuples(page_index)
        if current_page is None:
            current_page = []
        value_tuples = [astuple(value) for value in values]
        current_page += value_tuples
        self._write_page(current_page, page_index)

    def _write_page(self, data, i):
        """
        Serialise the data using JSON, compress it and store it at index i.
        If the data is too big, it will raise a ValueError and not store anything
        """
        if self.mode != 'w':
            raise UnsupportedOperation("The file is open in read mode, you cannot write")

        page_data = _get_page_data(self.compressor, self.page_size, data)
        self.mmap[i * self.page_size:(i+1) * self.page_size] = page_data

    @staticmethod
    def create(item_factory: Callable[..., T], index_path: str, num_pages: int, page_size: int):
        if os.path.isfile(index_path):
            raise FileExistsError(f"Index file '{index_path}' already exists")

        metadata = TinyIndexMetadata(VERSION, page_size, num_pages, item_factory.__name__)
        metadata_bytes = metadata.to_bytes()
        metadata_padded = _pad_to_page_size(metadata_bytes, METADATA_SIZE)

        compressor = ZstdCompressor()
        page_bytes = _get_page_data(compressor, page_size, [])

        with open(index_path, 'wb') as index_file:
            index_file.write(metadata_padded)
            for i in range(num_pages):
                index_file.write(page_bytes)

        return TinyIndex(item_factory, index_path=index_path)

