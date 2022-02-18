from pathlib import Path
from tempfile import TemporaryDirectory

from mwmbl.tinysearchengine.indexer import Document, TinyIndex


def test_create_index():
    num_pages = 10
    page_size = 4096

    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'temp-index.tinysearch'
        indexer = TinyIndex.create(Document, str(index_path), num_pages=num_pages, page_size=page_size)

        for i in range(num_pages):
            page = indexer.get_page(i)
            assert page == []
