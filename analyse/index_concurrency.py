import multiprocessing
import string
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory

from mwmbl.tinysearchengine.indexer import TinyIndex, Document


random = Random(2)


def random_string():
    randomly_generated_string = "".join([random.choice(string.ascii_letters) for _ in range(500)])
    return randomly_generated_string


DOCUMENTS = [
    Document(title=f"Document {i}", url=f"https://something.com/{i}.html", extract=random_string(), score=i) for
    i in range(1000)]

PAGES = [random.randint(0, 10240000) for _ in range(5)]

NUM_PAGES = 10240000
PAGE_SIZE = 4096

INDEX_PATH = 'temp-index.tinysearch'

try:
    TinyIndex.create(Document, str(INDEX_PATH), num_pages=NUM_PAGES, page_size=PAGE_SIZE)
except FileExistsError:
    pass

indexer = TinyIndex(Document, str(INDEX_PATH), 'w')
indexer.__enter__()


def read_or_write_page(i: int):
    page_index = random.choice(PAGES)
    if random.choice([True, False]):
        page = indexer.get_page(page_index)
        if len(page) > 0:
            pass
            # print(f"Read page {page_index}", page[0].url)
        else:
            print("Page is empty")
    else:
        sample = random.sample(DOCUMENTS, 500)
        # print(f"Storing in page {page_index}", sample[0].url)
        indexer.store_in_page(page_index, sample)


def index_reading_writing_multithreading():
    with multiprocessing.Pool(processes=20) as pool:
        print("Start")
        pool.map_async(read_or_write_page, [i for i in range(10000)])
        print("End")

        pool.close()
        pool.join()


if __name__ == '__main__':
    index_reading_writing_multithreading()
