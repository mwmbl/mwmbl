import json
import os
from dataclasses import dataclass, asdict, field
from enum import IntEnum
from io import UnsupportedOperation
from logging import getLogger
from typing import TypeVar, Generic, Callable, List, Optional

import mmh3
from lmdb_dict import SafeLmdbDict

VERSION = 1
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




class TinyIndex(Generic[T]):
    def __init__(self, item_factory: Callable[..., T], index_path, mode='r'):
        if mode not in {'r', 'w'}:
            raise ValueError(f"Mode should be one of 'r' or 'w', got {mode}")
        
        self.item_factory = item_factory
        self.index_path = index_path
        self.mode = mode
        self.lmdb_dict = None
        
        # Load metadata to get num_pages and page_size for compatibility
        try:
            temp_dict = SafeLmdbDict(index_path, map_size=2147483648)  # 2GB map size
            if "__metadata__" not in temp_dict:
                # Index exists but has no metadata - likely uninitialized
                # Create default metadata for an empty index
                default_num_pages = 100000
                default_page_size = PAGE_SIZE
                metadata = TinyIndexMetadata(
                    version=VERSION,
                    page_size=default_page_size, 
                    num_pages=default_num_pages,
                    item_factory=item_factory.__name__
                )
                logger.warning(f"No metadata found in index at {index_path}, using defaults: {default_num_pages} pages")
            else:
                metadata_dict = temp_dict["__metadata__"]
                metadata = TinyIndexMetadata(**metadata_dict)
        except Exception as e:
            # Database doesn't exist or is corrupted - create default metadata
            default_num_pages = 100000
            default_page_size = PAGE_SIZE
            metadata = TinyIndexMetadata(
                version=VERSION,
                page_size=default_page_size,
                num_pages=default_num_pages, 
                item_factory=item_factory.__name__
            )
            logger.warning(f"Failed to load index at {index_path} ({e}), using defaults: {default_num_pages} pages")
        
        if metadata.item_factory != item_factory.__name__:
            raise ValueError(f"Metadata item factory '{metadata.item_factory}' in the index "
                            f"does not match the passed item factory: '{item_factory.__name__}'")
        
        self.num_pages = metadata.num_pages
        self.page_size = metadata.page_size
        logger.info(f"Loaded LMDB index with {self.num_pages} pages")

    def __enter__(self):
        self.lmdb_dict = SafeLmdbDict(self.index_path, map_size=2147483648)  # 2GB map size
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lmdb_dict:
            # SafeLmdbDict handles its own cleanup
            self.lmdb_dict = None

    def retrieve(self, key: str) -> List[T]:
        index = self.get_key_page_index(key)
        logger.debug(f"Retrieving index {index}")
        page = self.get_page(index)
        return [item for item in page if item.term is None or item.term == key]

    def get_key_page_index(self, key) -> int:
        key_hash = mmh3.hash(key, signed=False)
        return key_hash % self.num_pages

    def get_page(self, i) -> list[T]:
        """Get the page at index i from LMDB and deserialise it"""
        page_key = f"page_{i}"
        if page_key not in self.lmdb_dict:
            return []
        
        # SafeLmdbDict automatically handles decompression and deserialization
        try:
            results = self.lmdb_dict[page_key]
            return [self.item_factory(*item) for item in results]
        except Exception as e:
            logger.exception(f"Error retrieving page {i}: {e}")
            return []

    def store_in_page(self, page_index: int, values: list[T]):
        if self.mode != 'w':
            raise UnsupportedOperation("The file is open in read mode, you cannot write")
            
        value_tuples = [value.as_tuple() for value in values]
        page_key = f"page_{page_index}"
        
        # SafeLmdbDict automatically handles compression and serialization
        self.lmdb_dict[page_key] = value_tuples

    @staticmethod
    def create(item_factory: Callable[..., T], index_path: str, num_pages: int, page_size: int):
        # Check if LMDB database already exists and has data
        try:
            temp_dict = SafeLmdbDict(index_path)
            if "__metadata__" in temp_dict:
                raise FileExistsError(f"Index database '{index_path}' already exists")
        except:
            pass  # Database doesn't exist yet, which is what we want
        
        # Create metadata
        metadata = TinyIndexMetadata(VERSION, page_size, num_pages, item_factory.__name__)
        
        # Initialize LMDB database with metadata
        lmdb_dict = SafeLmdbDict(index_path, map_size=2147483648)  # 2GB map size
        lmdb_dict["__metadata__"] = {
            'version': metadata.version,
            'page_size': metadata.page_size, 
            'num_pages': metadata.num_pages,
            'item_factory': metadata.item_factory
        }
        
        return TinyIndex(item_factory, index_path=index_path)

