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
from scipy.optimize import curve_fit, differential_evolution, minimize
from scipy.stats import binom, poisson, betabinom

from mwmbl.tinysearchengine.indexer import TinyIndex, Document

random = Random()

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

    return poisson_estimator_freq(frequencies, num_pages_observed, total_pages)


def poisson_estimator_freq(frequencies, num_pages_observed, total_pages):
    print("Frequencies", frequencies)
    freq = np.array(list(frequencies.keys()))
    values = np.array(list(frequencies.values()))

    m = np.dot(freq, values) / sum(values)
    print("Initial estimate", m)

    total_estimate = sum(values) * total_pages / num_pages_observed
    return total_estimate / m

    def log_likelihood(x):
        m1 = x
        logpmf = poisson.logpmf(freq, m1)
        return -np.dot(logpmf, values)

    def poiss(x, m1, s1):
        return poisson.pmf(x, m1) * s1

    res = minimize(log_likelihood, np.array([1.0]), method='L-BFGS-B')
    m1_fit = res.x
    print("Estimated parameter m", m1_fit)

    p0 = poisson.pmf(0, m1_fit)
    total_estimate = sum(frequencies.values())
    print("Total estimate", total_estimate)

    zero_estimate = p0 * total_estimate / (1 - p0)
    print("Estimated unseen URLs", zero_estimate)
    adjusted_total_estimate = total_estimate + zero_estimate * (1 - num_pages_observed / total_pages)
    return adjusted_total_estimate

    # # bounds = ([0, 0, 0, 0, 0, 0], [100, 100, 100, 1e10, 1e10, 1e10])
    # # m1_fit, s1_fit = curve_fit(poiss, freq, values, maxfev=50000)[0]
    # print("Estimated parameter m", m1_fit)
    # s1_fit = sum(frequencies.values())
    # predictions = poiss(freq, m1_fit, s1_fit)
    # print("Predictions", predictions.tolist())
    # print("Actual", values)
    # print("Differences", predictions - values)
    # total_estimate = sum(frequencies.values())
    # print("Total estimate", total_estimate)
    # zero_estimate = poiss(0, m1_fit, s1_fit)
    # print("Zero estimate", zero_estimate)
    # adjusted_total_estimate = total_estimate + zero_estimate * (1 - num_pages_observed / total_pages)
    # return adjusted_total_estimate


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
    # estimate = estimate_unique_urls(str(DEV_INDEX_PATH), 500)
    # print(f"Estimated number of unique URLs: {estimate}")
    #
    # num_urls = count_unique_urls(str(DEV_INDEX_PATH))
    # print(f"Actual number of unique URLs: {num_urls}")

    frequencies = {1: 10151439, 2: 91401, 3: 767, 4: 14, 5: 4, 6: 2, 7: 3, 9: 2, 10: 2, 14: 1}
    total_num_pages = 102400000
    estimate = poisson_estimator_freq(frequencies, total_num_pages * 0.005, total_num_pages)
    print(f"Estimated number of unique URLs: {estimate}")
