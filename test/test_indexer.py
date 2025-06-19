from pathlib import Path
from tempfile import TemporaryDirectory

from zstandard import ZstdCompressor

from mwmbl.tinysearchengine.indexer import TinyIndex, Document, _compress_data, VERSION


def test_create_index():
    num_pages = 10
    page_size = 4096

    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'temp-index.tinysearch'
        with TinyIndex.create(Document, str(index_path), num_pages=num_pages, page_size=page_size) as indexer:
            for i in range(num_pages):
                page = indexer.get_page(i)
                assert page == []

def test_compress_data_simple():
    """Test that _compress_data works with simple data"""
    items = [1, 2, 3, 4, 5]
    compressed_data = _compress_data(items)
    
    # Should return compressed bytes
    assert isinstance(compressed_data, bytes)
    assert len(compressed_data) > 0


def test_compress_data_with_documents():
    """Test that _compress_data works with Document objects"""
    document1 = Document(title='title1', url='url1', extract='extract1', score=1.0)
    document2 = Document(title='title2', url='url2', extract='extract2', score=2.0)
    items = [document1, document2]
    
    compressed_data = _compress_data(items)
    
    # Should return compressed bytes
    assert isinstance(compressed_data, bytes)
    assert len(compressed_data) > 0


def test_compress_data_empty():
    """Test that _compress_data works with empty list"""
    items = []
    compressed_data = _compress_data(items)
    
    # Should return compressed bytes even for empty data
    assert isinstance(compressed_data, bytes)
    assert len(compressed_data) > 0  # Even empty JSON arrays have some size when compressed


def test_constructing_document_removes_none():
    document = Document(title=None,url='url',extract=None,score=1.0)
    assert document.title == ''
    assert document.extract == ''
