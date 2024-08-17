from datetime import datetime, timezone
from multiprocessing.pool import ThreadPool
from pathlib import Path

import numpy as np
import pandas as pd
import requests

NUM_THREADS = 5

QUERIES_PATH = Path(__file__).parent.parent / 'devdata' / 'rankeval-2024-06' / 'queries.csv'
QUERY_URL = "http://localhost:5000/?q={query}"


np.random.seed(3)


def run():
    queries = pd.read_csv(QUERIES_PATH)
    query_sample = queries.sample(500)["suggestion"]
    print(query_sample)

    with ThreadPool(NUM_THREADS) as pool:
        times = pool.map(run_query, query_sample)

    print("Num threads\tNum queries\tMean seconds\tStd seconds\tMin seconds\tMax seconds")
    print(f"{NUM_THREADS}\t{len(query_sample)}\t{np.mean(times):.4f}\t{np.std(times):.4f}\t{np.min(times):.4f}\t{np.max(times):.4f}")


def run_query(query):
    start_time = datetime.now(timezone.utc)
    url = QUERY_URL.format(query=query)
    response = requests.get(url)
    print("Response", response.text[:100])
    end_time = datetime.now(timezone.utc)
    return (end_time - start_time).total_seconds()


if __name__ == '__main__':
    run()

