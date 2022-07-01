"""
Count unique URLs in the index.
"""
from mwmbl.tinysearchengine.indexer import TinyIndex, Document


def run():
    urls = set()
    with TinyIndex(Document, 'data/index.tinysearch') as index:
        for i in range(index.num_pages):
            print("Page", i)
            page = index.get_page(i)
            new_urls = {doc.url for doc in page}
            urls |= new_urls
    print("URLs", len(urls))


if __name__ == '__main__':
    run()
