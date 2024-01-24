"""
Create a bunch of requests to the back end to stress test it.
"""
import multiprocessing
from random import Random

import requests

random = Random(1)


def random_query(query):
    url = f"http://localhost:8000/search/?s={query}"
    print(url)
    response = requests.get(url)
    data = response.json()
    # print(data)


def run():
    print("Start")
    queries = ["".join(random.sample("the quick brown fox jumps over the lazy dog", 4)) for i in range(100000)]
    with multiprocessing.Pool(processes=20) as pool:
        pool.map(random_query, queries)
    print("End")


if __name__ == '__main__':
    run()
