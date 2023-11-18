"""
Investigate adding term information to the database.

How much extra space will it take?
"""
import os
from pathlib import Path
from random import Random

import numpy as np
from scipy.stats import sem

from mwmbl.tinysearchengine.indexer import TinyIndex, Document, _trim_items_to_page, astuple

from zstandard import ZstdCompressor

from mwmbl.utils import add_term_info

random = Random(1)

INDEX_PATH = Path(__file__).parent.parent / "devdata" / "index-v2.tinysearch"


def run():
    compressor = ZstdCompressor()
    with TinyIndex(Document, INDEX_PATH) as index:
        # Get some random integers between 0 and index.num_pages:
        pages = random.sample(range(index.num_pages), 10000)

        old_sizes = []
        new_sizes = []

        for i in pages:
            page = index.get_page(i)
            term_documents = []
            for document in page:
                term_document = add_term_info(document, index, i)
                term_documents.append(term_document)

            value_tuples = [astuple(value) for value in term_documents]
            num_fitting, compressed = _trim_items_to_page(compressor, index.page_size, value_tuples)

            new_sizes.append(num_fitting)
            old_sizes.append(len(page))

        print("Old sizes mean", np.mean(old_sizes), sem(old_sizes))
        print("New sizes mean", np.mean(new_sizes), sem(new_sizes))


if __name__ == '__main__':
    run()
