"""
Estimate the number of unique URLs in the index by fitting
a Binomial distribution to the data.
"""
from collections import Counter
from pathlib import Path
from random import Random

import numpy as np
from pydistinct.ensemble_estimators import median_estimator
from pydistinct.stats_estimators import bootstrap_estimator, goodmans_estimator, smoothed_jackknife_estimator, \
    horvitz_thompson_estimator
from scipy.optimize import curve_fit, differential_evolution
from scipy.stats import binom, poisson, betabinom

from mwmbl.tinysearchengine.indexer import TinyIndex, Document

random = Random(1)

DEV_INDEX_PATH = Path(__file__).parent.parent / "devdata" / "index-v2.tinysearch"


def count_unique_urls(index_path: str) -> int:
    with TinyIndex(Document, index_path) as index:
        urls = set()
        for i in range(index.num_pages):
            page = index.get_page(i)
            urls |= {doc.url for doc in page}
            if i % 1000 == 0:
                print(f"Processed {i} pages")
    return len(urls)


def estimate_unique_urls(index_path: str, num_pages_to_sample: int = 100):
    with TinyIndex(Document, index_path) as index:
        page_sample = set()
        while len(page_sample) < num_pages_to_sample:
            page_sample.add(random.randrange(index.num_pages))

        url_counts = Counter()
        total_docs = 0
        for i in page_sample:
            page = index.get_page(i)
            url_counts.update({doc.url for doc in page})
            total_docs += len(page)

    return median_estimator(attributes=dict(url_counts.items()))


if __name__ == "__main__":
    estimate = estimate_unique_urls(str(DEV_INDEX_PATH), 500)

    num_urls = count_unique_urls(str(DEV_INDEX_PATH))
    print(f"Actual number of unique URLs: {num_urls}")
