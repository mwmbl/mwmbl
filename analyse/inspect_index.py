from index import TinyIndex, Document, NUM_PAGES, PAGE_SIZE
from paths import INDEX_PATH


def get_items():
    tiny_index = TinyIndex(Document, INDEX_PATH, NUM_PAGES, PAGE_SIZE)
    items = tiny_index.retrieve('soup')
    if items:
        for item in items:
            print("Items", item)


def run():
    tiny_index = TinyIndex(Document, INDEX_PATH, NUM_PAGES, PAGE_SIZE)
    for i in range(100):
        tiny_index.get_page(i)


if __name__ == '__main__':
    run()
