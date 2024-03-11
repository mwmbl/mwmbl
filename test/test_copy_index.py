from pathlib import Path
from tempfile import TemporaryDirectory

from mwmbl.tinysearchengine.copy_index import copy_pages
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE

from django.conf import settings

NUM_PAGES = 10000


def test_copy_pages():
    search_term = "apple"
    old_index_path = Path(__file__).parent.parent / "devdata" / settings.INDEX_NAME

    # Make a temporary directory for the new index
    with TemporaryDirectory() as new_index_dir:
        new_index_path = str(Path(new_index_dir) / settings.INDEX_NAME)
        TinyIndex.create(Document, new_index_path, NUM_PAGES, PAGE_SIZE)
        with TinyIndex(item_factory=Document, index_path=old_index_path) as old_index:
            start_index = old_index.get_key_page_index(search_term)
            copy_pages(old_index_path, new_index_path, start_index, 1)
            with TinyIndex(item_factory=Document, index_path=new_index_path) as new_index:
                old_items = old_index.retrieve(search_term)
                new_items = new_index.retrieve(search_term)

    assert len(old_items) > 0

    old_urls = {item.url for item in old_items}
    new_urls = {item.url for item in new_items}

    # The scores may change, so we only compare the URLs
    assert old_urls == new_urls
