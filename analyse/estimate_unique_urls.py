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


def poisson_estimator(url_counts: dict[str, int], num_pages_observed: int, total_pages: int):
    print(f"Estimating for {num_pages_observed} pages observed.")
    count_frequencies = Counter(url_counts.values())

    frequencies = dict(sorted(count_frequencies.items()))
    max_freq = max(frequencies.keys())
    # for i in range(1, 100):
    #     frequencies[max_freq + i] = 0

    freq = np.array(list(frequencies.keys()))
    values = np.array(list(frequencies.values()))

    def poiss(x, m1, m2, s1, s2):
        return poisson.pmf(x, m1) * s1 + poisson.pmf(x, m2) * s2

    # bounds = ([0, 0, 0, 0, 0, 0], [100, 100, 100, 1e10, 1e10, 1e10])
    m1_fit, m2_fit, s1_fit, s2_fit = curve_fit(poiss, freq, values, maxfev=10000)[0]
    print("Estimated parameter m", m1_fit, m2_fit, s1_fit, s2_fit)

    predictions = poiss(freq, m1_fit, m2_fit, s1_fit, s2_fit)
    print("Predictions", predictions.tolist())
    print("Actual", values)
    print("Differences", predictions - values)

    total_estimate = len(url_counts)
    print("Total estimate", total_estimate)

    zero_estimate = poiss(0, m1_fit, m2_fit, s1_fit, s2_fit)
    print("Zero estimate", zero_estimate)

    adjusted_total_estimate = total_estimate + zero_estimate * (1 - num_pages_observed / total_pages)
    return adjusted_total_estimate


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
