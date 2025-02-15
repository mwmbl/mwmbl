from pathlib import Path
from tempfile import TemporaryDirectory

from zstandard import ZstdCompressor

from mwmbl.tinysearchengine.indexer import TinyIndex, Document, _binary_search_fitting_size, \
    _trim_items_to_page, _pad_to_page_size, _get_page_data


def test_create_index():
    num_pages = 10
    page_size = 4096

    with TemporaryDirectory() as temp_dir:
        index_path = Path(temp_dir) / 'temp-index.tinysearch'
        with TinyIndex.create(Document, str(index_path), num_pages=num_pages, page_size=page_size) as indexer:
            for i in range(num_pages):
                page = indexer.get_page(i)
                assert page == []

def test_binary_search_fitting_size_all_fit():
    items = [1,2,3,4,5,6,7,8,9]
    compressor = ZstdCompressor()
    page_size = 4096
    count_fit, data = _binary_search_fitting_size(compressor,page_size,items,0,len(items))
    
    # We should fit everything
    assert count_fit == len(items)
    
def test_binary_search_fitting_size_subset_fit():
    items = [1,2,3,4,5,6,7,8,9]
    compressor = ZstdCompressor()
    page_size = 15
    count_fit, data = _binary_search_fitting_size(compressor,page_size,items,0,len(items))
    
    # We should not fit everything
    assert count_fit < len(items)
    
def test_binary_search_fitting_size_none_fit():
    items = [1,2,3,4,5,6,7,8,9]
    compressor = ZstdCompressor()
    page_size = 5
    count_fit, data = _binary_search_fitting_size(compressor,page_size,items,0,len(items))
    
    # We should not fit anything
    assert count_fit == -1
    assert data is None


def test_get_page_data_single_doc():
    document1 = Document(title='title1',url='url1',extract='extract1',score=1.0)
    items = [document1.as_tuple()]

    compressor = ZstdCompressor()
    page_size = 4096
    
    # Trim data
    num_fitting,trimmed_data = _trim_items_to_page(compressor,4096,items)
    
    # We should be able to fit the 1 item into a page
    assert num_fitting == 1
    
    # Compare the trimmed data to the actual data we're persisting
    # We need to pad the trimmmed data, then it should be equal to the data we persist
    padded_trimmed_data = _pad_to_page_size(trimmed_data, page_size)
    serialized_data = _get_page_data(page_size, items)
    assert serialized_data == padded_trimmed_data
    

def test_get_page_data_many_docs_all_fit():
    # Build giant documents item
    documents = []
    documents_len = 500
    page_size = 4096
    for x in range(documents_len):
        txt = 'text{}'.format(x)
        document = Document(title=txt,url=txt,extract=txt,score=x)
        documents.append(document)
    items = [document.as_tuple() for document in documents]
    
    # Trim the items
    compressor = ZstdCompressor()
    num_fitting,trimmed_data = _trim_items_to_page(compressor,page_size,items)
    
    # We should be able to fit all items
    assert num_fitting == documents_len
    
    # Compare the trimmed data to the actual data we're persisting
    # We need to pad the trimmed data, then it should be equal to the data we persist
    serialized_data = _get_page_data(page_size, items)
    padded_trimmed_data = _pad_to_page_size(trimmed_data, page_size)
    
    assert serialized_data == padded_trimmed_data


def test_get_page_data_many_docs_subset_fit():
    # Build giant documents item
    documents = []
    documents_len = 5000
    page_size = 4096
    for x in range(documents_len):
        txt = 'text{}'.format(x)
        document = Document(title=txt,url=txt,extract=txt,score=x)
        documents.append(document)
    items = [document.as_tuple() for document in documents]
    
    # Trim the items
    compressor = ZstdCompressor()
    num_fitting,trimmed_data = _trim_items_to_page(compressor,page_size,items)
    
    # We should be able to fit a subset of the items onto the page
    assert num_fitting > 1
    assert num_fitting < documents_len
    
    # Compare the trimmed data to the actual data we're persisting
    # We need to pad the trimmed data, then it should be equal to the data we persist
    serialized_data = _get_page_data(page_size, items)
    padded_trimmed_data = _pad_to_page_size(trimmed_data, page_size)
    
    assert serialized_data == padded_trimmed_data


def test_constructing_document_removes_none():
    document = Document(title=None,url='url',extract=None,score=1.0)
    assert document.title == ''
    assert document.extract == ''
