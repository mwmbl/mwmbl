"""
Estimate the number of unique URLs in the index by fitting
a Binomial distribution to the data.
"""
from collections import Counter
from pathlib import Path
from random import Random

import pymc as pm
import numpy as np
from pydistinct.ensemble_estimators import median_estimator
from pydistinct.stats_estimators import bootstrap_estimator, goodmans_estimator, smoothed_jackknife_estimator, \
    horvitz_thompson_estimator
from scipy.optimize import curve_fit, differential_evolution
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


MAX_TOKENS = 25


def binomial_estimator(url_counts: dict[str, int], mean_urls_per_page: float, num_pages_observed: int, total_pages: int):
    print(f"Estimating for {mean_urls_per_page} mean urls per page and {num_pages_observed} pages observed.")
    with pm.Model() as model:
        p = pm.Beta("p", alpha=1000, beta=1)
        pm.Binomial("obs", n=num_pages_observed, p=p, observed=np.array(list(url_counts.values())))

        trace = pm.sample(1000)
        print("Trace", trace.posterior["p"])
        mean_tokens_per_url = trace.posterior["p"].mean() * total_pages
        print("Estimated mean tokens per URL", mean_tokens_per_url)
        return mean_urls_per_page * total_pages / mean_tokens_per_url


def poisson_estimator(url_counts: dict[str, int], mean_urls_per_page: float, num_pages_observed: int, total_pages: int):
    print(f"Estimating for {mean_urls_per_page} mean urls per page and {num_pages_observed} pages observed.")
    count_frequencies = Counter(url_counts.values())

    frequencies = dict(sorted(count_frequencies.items()))
    max_freq = max(frequencies.keys())
    # for i in range(1, 100):
    #     frequencies[max_freq + i] = 0

    freq = np.array(list(frequencies.keys()))
    values = np.array(list(frequencies.values()))

    def poiss(x, m1, m2, m3, s1, s2, s3):
        return poisson.pmf(x, m1) * s1 + poisson.pmf(x, m2) * s2 + poisson.pmf(x, m3) * s3
    m1_fit, m2_fit, m3_fit, s1_fit, s2_fit, s3_fit = curve_fit(poiss, freq, values)[0]
    print("Estimated parameter m", m1_fit, m2_fit, m3_fit, s1_fit, s2_fit, s3_fit)

    predictions = poiss(freq, m1_fit, m2_fit, m3_fit, s1_fit, s2_fit, s3_fit)
    print("Predictions", predictions.tolist())
    print("Actual", values)
    print("Differences", predictions - values)

    pages_per_url = (m1_fit * s1_fit + m2_fit * s2_fit + m3_fit * s3_fit) / (s1_fit + s2_fit + s3_fit)
    print("Pages per url", pages_per_url)

    input_freq = np.arange(100)
    predictions = poiss(input_freq, m1_fit, m2_fit, m3_fit, s1_fit, s2_fit, s3_fit)

    # Adjust the zero prediction since we have already seen some of these URLs
    predictions[0] *= (1 - num_pages_observed / total_pages)

    pages_per_url_mean = ((input_freq * predictions) / sum(predictions)).sum()
    print("Pages per url mean", pages_per_url_mean)

    adjusted_pages_per_url = pages_per_url_mean * total_pages / num_pages_observed
    print("Adjusted pages per url", adjusted_pages_per_url)

    total_urls = mean_urls_per_page * total_pages
    return total_urls / adjusted_pages_per_url


def binomial_mixture_estimator(url_counts: dict[str, int], mean_urls_per_page: float, num_pages_observed: int, total_pages: int):
    print(f"Estimating for {mean_urls_per_page} mean urls per page and {num_pages_observed} pages observed.")
    count_frequencies = Counter(url_counts.values())

    frequencies = dict(sorted(count_frequencies.items()))
    max_freq = max(frequencies.keys())
    frequencies[max_freq + 1] = 0

    freq = np.array(list(frequencies.keys()))
    values = np.array(list(frequencies.values()))

    def binomial(x, p1, s1, p2, s2):
        return binom.pmf(x, 25, p1) * s1 + binom.pmf(x, 25, p2) * s2

    bounds = ([0, 1, 0, 1], [1, 1e6, 1, 1e6])
    p1, s1, p2, s2 = curve_fit(binomial, freq, values, bounds=bounds)[0]
    print("Estimated parameter p", p1, s1, p2, s2)

    predictions = binomial(freq, p1, s1, p2, s2)
    print("Predictions", predictions.tolist())
    print("Actual", values)
    print("Differences", (predictions - values).tolist())

    pages_per_url = (p1 * s1 + p2 * s2) * 25 / (s1 + s2)
    print("Pages per url", pages_per_url)

    adjusted_pages_per_url = pages_per_url * total_pages / num_pages_observed
    print("Adjusted pages per url", adjusted_pages_per_url)

    total_urls = mean_urls_per_page * total_pages
    return total_urls / adjusted_pages_per_url





def estimate_unique_urls(index_path: str, num_pages_to_sample: int = 100):
    with TinyIndex(Document, index_path) as index:
        total_pages = index.num_pages
        page_sample = set()
        while len(page_sample) < num_pages_to_sample:
            page_sample.add(random.randrange(index.num_pages))

        url_counts = Counter()
        total_docs = 0
        for i in page_sample:
            page = index.get_page(i)
            url_counts.update({doc.url for doc in page})
            total_docs += len(page)

    return poisson_estimator(url_counts, mean_urls_per_page=total_docs / num_pages_to_sample,
                              num_pages_observed=num_pages_to_sample, total_pages=total_pages)
    # return median_estimator(attributes=dict(url_counts.items()))


if __name__ == "__main__":
    estimates = []
    for i in range(10):
        estimate = estimate_unique_urls(str(DEV_INDEX_PATH), 2000)
        estimates.append(estimate)
    print(f"Estimated number of unique URLs: {np.mean(estimates)} ± {np.std(estimates)}")

    num_urls = count_unique_urls(str(DEV_INDEX_PATH))
    print(f"Actual number of unique URLs: {num_urls}")
