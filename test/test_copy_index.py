from pathlib import Path
from tempfile import TemporaryDirectory

from mwmbl.tinysearchengine.copy_index import copy_pages
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, PAGE_SIZE

from django.conf import settings

NUM_PAGES = 10000


def test_copy_pages():
    search_term = "apple"
    
    # Create temporary directories for both old and new indexes
    with TemporaryDirectory() as temp_dir:
        old_index_path = str(Path(temp_dir) / "old_index.lmdb")
        new_index_path = str(Path(temp_dir) / "new_index.lmdb")
        
        # Create and populate the old index with test data
        test_documents = [
            Document(title="Apple Fruit", url="https://apple.com", extract="Red fruit", score=1.0, term=search_term),
            Document(title="Apple Company", url="https://apple-inc.com", extract="Tech company", score=0.8, term=search_term),
            Document(title="Green Apple", url="https://green-apple.com", extract="Green variety", score=0.9),
        ]
        
        with TinyIndex.create(Document, old_index_path, NUM_PAGES, PAGE_SIZE) as old_index:
            # Store test documents in the page that the search term hashes to
            page_index = old_index.get_key_page_index(search_term)
            old_index.store_in_page(page_index, test_documents)
        
        # Create the new index
        TinyIndex.create(Document, new_index_path, NUM_PAGES, PAGE_SIZE)
        
        # Test copying pages
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
