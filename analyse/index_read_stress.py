"""
Stress test the index by reading from random pages in parallel.
"""
import multiprocessing
import os
from multiprocessing.pool import ThreadPool
from pathlib import Path
from random import Random

from mwmbl.tinysearchengine.indexer import TinyIndex, Document


random = Random(1)

INDEX_PATH = Path(os.environ["HOME"]) / 'mwmbl-data' / 'index-v2.tinysearch'
index = TinyIndex(Document, str(INDEX_PATH), 'r')
index.__enter__()


def read_page(i: int):
    page_index = random.randint(0, index.num_pages - 1)
    # page_index = random.choice([8462157, 2661923, 7147655])
    try:
        page = index.get_page(page_index)
        # print("Page", page_index, page)
    # except ValueError:
    #     return
    except Exception as e:
        print(e)
        # return

    if random.randint(0, 100) == 0:
        index.a = random.randint(0, 1000)

    print("Index", getattr(index, 'a', None))

    # if len(page) > 0:
    #     pass
    #     print(f"Read page {page_index}", page[0].url)
    # else:
    #     print("Page is empty")


def run():
    print("Start")
    for i in range(10):
        print(f"Starting batch {i}")
        with multiprocessing.Pool(processes=20) as pool:
        # with ThreadPool(processes=20) as pool:
            pool.map(read_page, range(100000))
    print("End")


if __name__ == '__main__':
    run()
