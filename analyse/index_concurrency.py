import multiprocessing
import os
import string
from multiprocessing.pool import ThreadPool
from pathlib import Path
from random import Random

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

INDEX_PATH = Path(os.environ["HOME"] + "/mwmbl-data") / "index-v2.tinysearch"

# INDEX_PATH = 'temp-index.tinysearch'

try:
    TinyIndex.create(Document, str(INDEX_PATH), num_pages=NUM_PAGES, page_size=PAGE_SIZE)
except FileExistsError:
    pass

indexer = TinyIndex(Document, str(INDEX_PATH), 'r')
indexer.__enter__()


def read_page(page_index: int):
    try:
        page = indexer.get_page(page_index)
    except Exception as e:
        print(e)
        return


def index_reading_writing_multithreading():
    # with multiprocessing.Pool(processes=10) as pool:
    with ThreadPool(processes=10) as pool:
        print("Start")
        pool.map_async(read_page, [random.randint(0, indexer.num_pages) for i in range(1000000)])

        pool.close()
        pool.join()
        print("End")


if __name__ == '__main__':
    index_reading_writing_multithreading()
