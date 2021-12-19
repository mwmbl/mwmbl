from index import TinyIndex, Document, NUM_PAGES, PAGE_SIZE
from paths import INDEX_PATH


def run():
    tiny_index = TinyIndex(Document, INDEX_PATH, NUM_PAGES, PAGE_SIZE)
    for i in range(100):
        items = tiny_index.retrieve('eggless')
        # items = tiny_index.convert_items(page)
        if items:
            print("Items", items)
            break


if __name__ == '__main__':
    run()
