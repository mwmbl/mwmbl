"""
Estimate the number of unique URLs in the index by fitting
a Binomial distribution to the data.
"""
from collections import Counter
from pathlib import Path
from random import Random

import numpy as np
from scipy.optimize import curve_fit, differential_evolution
from scipy.stats import binom, poisson

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

        frequencies = Counter(url_counts.values())
        print("Frequencies", frequencies)

        # Fit a binomial distribution to the data
        def binomial(x, p, s):
            print("Binomial", x, p, s)
            return binom(num_pages_to_sample, p).pmf(x) * s

        frequencies = dict(sorted(frequencies.items()))
        freq = np.array(list(frequencies.keys()))
        values = np.array(list(frequencies.values()))

        def poiss(x, m, s):
            return poisson.pmf(x, m) * s

        m_fit, s_fit = curve_fit(poiss, freq, values)[0]
        print("Estimated parameter m", m_fit, s_fit)

        predictions = poiss(freq, m_fit, s_fit)
        print("Predictions", predictions)
        print("Actual", values)


        #
        # def cost_function(x):
        #     p, s = x
        #     value = np.sum((binomial(freq, p, s) - values) ** 2)
        #     return value
        #
        # result = differential_evolution(cost_function, bounds=[(0.0, 1.0), (1.0, 1e100)], args=(), maxiter=100_000, strategy='rand2bin', tol=0.001)
        # print("Result", result)
        #
        # p, s = result.x
        # print("Predicted values", binomial(freq, p, s))
        # print("Actual", values)

        # num_unique_urls = len(url_counts)
        # mean_results_per_page = total_docs / num_pages_to_sample
        # initial_p = mean_results_per_page / num_unique_urls
        # print("Initial p", initial_p)
        # print("Initial s", num_unique_urls)
        # p0 = [initial_p, num_unique_urls]
        # f0 = binomial(freq, *p0)
        # print("F0", f0)
        #
        # p_fit, s_fit = curve_fit(binomial, freq, values, bounds=[(0.0, 1.0), (1.0, 1e100)],
        #                          p0=p0)[0]
        # print("Estimated parameters p, s", p_fit, s_fit)


if __name__ == "__main__":
    estimate = estimate_unique_urls(str(DEV_INDEX_PATH), 1000)

    num_urls = count_unique_urls(str(DEV_INDEX_PATH))
    print(f"Actual number of unique URLs: {num_urls}")
