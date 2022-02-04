import json
import os
from dataclasses import astuple, dataclass
from mmap import mmap, PROT_READ
from pathlib import Path
from typing import TypeVar, Generic, Callable, List

import mmh3
from zstandard import ZstdDecompressor, ZstdCompressor, ZstdError


NUM_PAGES = 25600
PAGE_SIZE = 4096


@dataclass
class Document:
    title: str
    url: str
    extract: str


@dataclass
class TokenizedDocument(Document):
    tokens: List[str]


T = TypeVar('T')


class TinyIndexBase(Generic[T]):
    def __init__(self, item_factory: Callable[..., T], num_pages: int, page_size: int):
        self.item_factory = item_factory
        self.num_pages = num_pages
        self.page_size = page_size
        self.decompressor = ZstdDecompressor()
        self.mmap = None

    def retrieve(self, key: str) -> List[T]:
        index = self._get_key_page_index(key)
        page = self.get_page(index)
        if page is None:
            return []
        return self.convert_items(page)

    def _get_key_page_index(self, key):
        key_hash = mmh3.hash(key, signed=False)
        return key_hash % self.num_pages

    def get_page(self, i):
        """
        Get the page at index i, decompress and deserialise it using JSON
        """
        page_data = self.mmap[i * self.page_size:(i + 1) * self.page_size]
        try:
            decompressed_data = self.decompressor.decompress(page_data)
        except ZstdError:
            return None
        results = json.loads(decompressed_data.decode('utf8'))
        return results

    def convert_items(self, items) -> List[T]:
        converted = [self.item_factory(*item) for item in items]
        return converted


class TinyIndex(TinyIndexBase[T]):
    def __init__(self, item_factory: Callable[..., T], index_path, num_pages, page_size):
        super().__init__(item_factory, num_pages, page_size)
        self.index_path = index_path
        self.index_file = open(self.index_path, 'rb')
        self.mmap = mmap(self.index_file.fileno(), 0, prot=PROT_READ)


class TinyIndexer(TinyIndexBase[T]):
    def __init__(self, item_factory: Callable[..., T], index_path: str, num_pages: int, page_size: int):
        super().__init__(item_factory, num_pages, page_size)
        self.index_path = index_path
        self.compressor = ZstdCompressor()
        self.decompressor = ZstdDecompressor()
        self.index_file = None
        self.mmap = None

    def __enter__(self):
        self.create_if_not_exists()
        self.index_file = open(self.index_path, 'r+b')
        self.mmap = mmap(self.index_file.fileno(), 0)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.mmap.close()
        self.index_file.close()

    def index(self, key: str, value: T):
        # print("Index", value)
        assert type(value) == self.item_factory, f"Can only index the specified type" \
                                              f" ({self.item_factory.__name__})"
        page_index = self._get_key_page_index(key)
        current_page = self.get_page(page_index)
        if current_page is None:
            current_page = []
        value_tuple = astuple(value)
        # print("Value tuple", value_tuple)
        current_page.append(value_tuple)
        try:
            # print("Page", current_page)
            self._write_page(current_page, page_index)
        except ValueError:
            pass

    def _write_page(self, data, i):
        """
        Serialise the data using JSON, compress it and store it at index i.
        If the data is too big, it will raise a ValueError and not store anything
        """
        serialised_data = json.dumps(data)
        compressed_data = self.compressor.compress(serialised_data.encode('utf8'))
        page_length = len(compressed_data)
        if page_length > self.page_size:
            raise ValueError(f"Data is too big ({page_length}) for page size ({self.page_size})")
        padding = b'\x00' * (self.page_size - page_length)
        self.mmap[i * self.page_size:(i+1) * self.page_size] = compressed_data + padding

    def create_if_not_exists(self):
        if not os.path.isfile(self.index_path):
            file_length = self.num_pages * self.page_size
            with open(self.index_path, 'wb') as index_file:
                index_file.write(b'\x00' * file_length)
