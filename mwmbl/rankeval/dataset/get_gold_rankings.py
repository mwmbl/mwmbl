"""
Retrieve the "gold standard" rankings from Bing Web Search.
"""

import os

import pandas as pd
from pandas import DataFrame
from requests import HTTPError

from mwmbl.rankeval.paths import QUERIES_DATASET_PATH, RANKINGS_DATASET_TRAIN_PATH, RANKINGS_DATASET_TEST_PATH
from mwmbl.rankeval.dataset.search_api import retrieve_rankings

BING_API_SUBSCRIPTION_KEY = os.environ['BING_API_SUBSCRIPTION_KEY']
BING_SEARCH_API_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"
BING_SUGGEST_API_ENDPOINT = "https://api.bing.microsoft.com/v7.0/Suggestions"


NUM_QUERIES = 10000


def get_query_rankings(queries) -> DataFrame:
    print("Queries", len(queries))

    dataset = []
    for query in queries:
        try:
            rankings = retrieve_rankings(query)
        except (HTTPError, KeyError) as e:
            print("Error getting rankings", e)
            continue
        print("Rankings", len(dataset))
        rankings_df = DataFrame(rankings)
        rankings_df['query'] = query
        dataset.append(rankings_df)
    return pd.concat(dataset)


def run():
    query_dataset = pd.read_csv(QUERIES_DATASET_PATH)

    # Get one suggestion for each query
    # Use this method: https://stackoverflow.com/a/46660098
    # Shuffle, then take the top item from each group
    queries = query_dataset.sample(frac=1.0, random_state=1)\
        .groupby('query')\
        .head()['suggestion']\
        .to_list()

    for i, path in enumerate([RANKINGS_DATASET_TRAIN_PATH, RANKINGS_DATASET_TEST_PATH]):
        if i == 0:
            continue
        rankings = get_query_rankings(queries[NUM_QUERIES * i:NUM_QUERIES * (i+1)])
        rankings.to_csv(path)


if __name__ == '__main__':
    run()
