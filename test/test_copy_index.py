from pathlib import Path
from tempfile import TemporaryDirectory

from mwmbl.indexer.paths import INDEX_NAME
from mwmbl.tinysearchengine.copy_index import copy_pages
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE

NUM_PAGES = 10000


def test_copy_pages():
    search_term = "apple"
    old_index_path = Path(__file__).parent.parent / "devdata" / INDEX_NAME

    # Make a temporary directory for the new index
    with TemporaryDirectory() as new_index_dir:
        new_index_path = str(Path(new_index_dir) / INDEX_NAME)
        TinyIndex.create(Document, new_index_path, NUM_PAGES, PAGE_SIZE)
        with TinyIndex(item_factory=Document, index_path=old_index_path) as old_index:
            start_index = old_index.get_key_page_index(search_term)
            copy_pages(old_index_path, new_index_path, start_index, 1)
            with TinyIndex(item_factory=Document, index_path=new_index_path) as new_index:
                old_items = old_index.retrieve(search_term)
                new_items = new_index.retrieve(search_term)

    assert len(old_items) > 0
    assert old_items == new_items
