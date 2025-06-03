import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from mwmbl.tinysearchengine.indexer import TinyIndex, Document, DocumentState


def test_create_index():
    """Test creating a new LMDB-based TinyIndex."""
    num_pages = 10
    page_size = 4096

    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'temp-index.lmdb'
        
        # Create the index
        with TinyIndex.create(Document, str(index_path), num_pages=num_pages, page_size=page_size) as indexer:
            # All pages should be empty initially
            for i in range(min(5, num_pages)):  # Test first 5 pages
                page = indexer.get_page(i)
                assert page == []


def test_store_and_retrieve_documents():
    """Test storing documents in pages and retrieving them."""
    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'test-index.lmdb'
        
        # Create documents to store
        docs = [
            Document(title='Title 1', url='https://example1.com', extract='Extract 1', score=1.0),
            Document(title='Title 2', url='https://example2.com', extract='Extract 2', score=2.0),
            Document(title='Title 3', url='https://example3.com', extract='Extract 3', score=3.0),
        ]
        
        # Create index and store documents
        with TinyIndex.create(Document, str(index_path), num_pages=100, page_size=4096) as index:
            index.store_in_page(0, docs)
            
            # Retrieve the page
            retrieved_docs = index.get_page(0)
            
            assert len(retrieved_docs) == 3
            assert retrieved_docs[0].title == 'Title 1'
            assert retrieved_docs[0].url == 'https://example1.com'
            assert retrieved_docs[1].title == 'Title 2'
            assert retrieved_docs[2].title == 'Title 3'


def test_retrieve_by_key():
    """Test retrieving documents by key using the retrieve method."""
    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'test-index.lmdb'
        
        # Create documents with specific terms
        docs = [
            Document(title='Python Guide', url='https://python.org', extract='Learn Python', score=1.0, term='python'),
            Document(title='Django Tutorial', url='https://django.com', extract='Web framework', score=2.0, term='django'),
            Document(title='General Doc', url='https://general.com', extract='General content', score=3.0),
        ]
        
        with TinyIndex.create(Document, str(index_path), num_pages=100, page_size=4096) as index:
            # Store documents in the page that 'python' key hashes to
            python_page = index.get_key_page_index('python')
            index.store_in_page(python_page, docs)
            
            # Retrieve documents for 'python' key
            python_results = index.retrieve('python')
            
            # Should return both the python-specific doc and the general doc (no term)
            assert len(python_results) >= 1
            python_docs = [doc for doc in python_results if doc.term == 'python']
            assert len(python_docs) == 1
            assert python_docs[0].title == 'Python Guide'


def test_key_page_index_hashing():
    """Test that key hashing consistently maps to the same page."""
    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'test-index.lmdb'
        
        with TinyIndex.create(Document, str(index_path), num_pages=100, page_size=4096) as index:
            # Same key should always map to same page
            page1 = index.get_key_page_index('test')
            page2 = index.get_key_page_index('test')
            assert page1 == page2
            
            # Different keys should (usually) map to different pages
            page_a = index.get_key_page_index('apple')
            page_b = index.get_key_page_index('banana')
            # Note: could theoretically be same due to hash collisions, but very unlikely
            
            # Page indices should be within valid range
            assert 0 <= page1 < 100
            assert 0 <= page_a < 100
            assert 0 <= page_b < 100


def test_open_existing_index():
    """Test opening an existing index and reading metadata."""
    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'test-index.lmdb'
        
        # Create index with specific parameters
        num_pages = 50
        page_size = 2048
        
        # Create and populate index
        with TinyIndex.create(Document, str(index_path), num_pages=num_pages, page_size=page_size) as index:
            docs = [Document(title='Test', url='https://test.com', extract='Test extract', score=1.0)]
            index.store_in_page(5, docs)
        
        # Open existing index
        with TinyIndex(Document, str(index_path), mode='r') as index:
            assert index.num_pages == num_pages
            assert index.page_size == page_size
            
            # Should be able to read stored data
            retrieved_docs = index.get_page(5)
            assert len(retrieved_docs) == 1
            assert retrieved_docs[0].title == 'Test'


def test_document_as_tuple():
    """Test Document.as_tuple() method converts document to tuple format."""
    doc = Document(
        title='Test Title',
        url='https://test.com',
        extract='Test extract',
        score=1.5,
        term='test',
        state=DocumentState.FROM_USER
    )
    
    tuple_repr = doc.as_tuple()
    
    # Should be a tuple with all fields, state converted to int value
    assert isinstance(tuple_repr, tuple)
    assert tuple_repr[0] == 'Test Title'
    assert tuple_repr[1] == 'https://test.com'
    assert tuple_repr[2] == 'Test extract'
    assert tuple_repr[3] == 1.5
    assert tuple_repr[4] == 'test'
    assert tuple_repr[5] == DocumentState.FROM_USER.value


def test_document_as_tuple_truncates_none():
    """Test that as_tuple() truncates trailing None values."""
    doc = Document(title='Test', url='https://test.com', extract='Extract', score=1.0)
    # term and state are None
    
    tuple_repr = doc.as_tuple()
    
    # Should not include None values at the end
    assert len(tuple_repr) == 4  # title, url, extract, score
    assert tuple_repr[-1] == 1.0  # score should be last


def test_constructing_document_removes_none():
    """Test Document constructor handles None values for title and extract."""
    document = Document(title=None, url='url', extract=None, score=1.0)
    assert document.title == ''
    assert document.extract == ''


def test_empty_page_retrieval():
    """Test retrieving from an empty page returns empty list."""
    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'test-index.lmdb'
        
        with TinyIndex.create(Document, str(index_path), num_pages=100, page_size=4096) as index:
            # Get a page that has never been written to
            empty_page = index.get_page(50)
            assert empty_page == []


def test_write_mode_restriction():
    """Test that writing requires write mode."""
    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'test-index.lmdb'
        
        # Create index first
        with TinyIndex.create(Document, str(index_path), num_pages=10, page_size=4096):
            pass
        
        # Open in read mode
        with TinyIndex(Document, str(index_path), mode='r') as index:
            docs = [Document(title='Test', url='https://test.com', extract='Test', score=1.0)]
            
            # Should raise exception when trying to write in read mode
            with pytest.raises(Exception):  # UnsupportedOperation
                index.store_in_page(0, docs)


def test_index_already_exists_error():
    """Test that creating an index that already exists raises FileExistsError."""
    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'test-index.lmdb'
        
        # Create index first time
        with TinyIndex.create(Document, str(index_path), num_pages=10, page_size=4096):
            pass
        
        # Try to create again - should raise error
        with pytest.raises(FileExistsError):
            TinyIndex.create(Document, str(index_path), num_pages=10, page_size=4096)
