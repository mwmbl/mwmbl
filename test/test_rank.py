from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import order_results


def test_order_result():
    doc1 = Document(title='title2', url='https://something.com', extract='extract2', score=2.0)
    doc2 = Document(title='title3', url='https://something.com', extract='extract3', score=3.0)
    doc3 = Document(title='Bananas and apples', url='https://something.com', extract='extract1', score=1.0)

    documents = [doc1, doc2, doc3]

    # Sort the documents
    ordered_results = order_results(["bananas"], documents, True)

    assert ordered_results[0].title == 'Bananas and apples'
