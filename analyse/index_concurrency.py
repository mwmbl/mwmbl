import multiprocessing
import os
import string
from pathlib import Path
from random import Random

from mwmbl.indexer.paths import INDEX_NAME
from mwmbl.tinysearchengine.indexer import TinyIndex, Document

random = Random(2)


def random_string():
    randomly_generated_string = "".join([random.choice(string.ascii_letters) for _ in range(500)])
    return randomly_generated_string


DOCUMENTS = [
    Document(title=f"Document {i}", url=f"https://something.com/{i}.html", extract=random_string(), score=i) for
    i in range(1000)]

PAGES = [random.randint(0, 10240000) for _ in range(20)]

NUM_PAGES = 10240000
PAGE_SIZE = 4096

INDEX_PATH = Path(os.environ["HOME"] + "/mwmbl-data") / INDEX_NAME

# INDEX_PATH = 'temp-index.tinysearch'

try:
    TinyIndex.create(Document, str(INDEX_PATH), num_pages=NUM_PAGES, page_size=PAGE_SIZE, checksum_size=0)
except FileExistsError:
    pass

indexer = TinyIndex(Document, str(INDEX_PATH), 'r')
indexer.__enter__()


def read_page(page_index: int):
    # page_index = random.choice(PAGES)
    try:
        page = indexer.get_page(page_index)
    except Exception as e:
        print(e)
        return
    # if len(page) > 0:
    #     pass
    #     print(f"Read page {page_index}", page[0].url)
    # else:
    #     print("Page is empty")


# def write_page():
#     page_index = random.choice(PAGES)
#     sample = random.sample(DOCUMENTS, 500)
#     # print(f"Storing in page {page_index}", sample[0].url)
#     indexer.store_in_page(page_index, sample)


def index_reading_writing_multithreading():
    with multiprocessing.Pool(processes=10) as pool:
        print("Start")
        pool.map_async(read_page, [random.randint(0, indexer.num_pages) for i in range(1000000)])

        # for i in range(1000):
        #     write_page()

        pool.close()
        pool.join()


if __name__ == '__main__':
    index_reading_writing_multithreading()
