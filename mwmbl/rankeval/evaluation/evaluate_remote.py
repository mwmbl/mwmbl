"""
Evaluate against queries on a remote search engine.
"""

from argparse import ArgumentParser

import requests
from joblib import Memory

from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import Ranker, HeuristicRanker

from mwmbl.rankeval.evaluation.evaluate import RankingModel, evaluate


memory = Memory(location="devdata/cache")


@memory.cache
def fetch_results(url: str, query: str):
    results = requests.get(url + "/api/v1/search/?s=" + query).json()
    return results


class RemoteRankingModel(RankingModel):
    def __init__(self, url: str = "https://mwmbl.org"):
        self.url = url

    def predict(self, query: str) -> list[str]:
        results = fetch_results(self.url, query)
        return [x['url'] for x in results]


def run():
    model = RemoteRankingModel()
    evaluate(model, fraction=1.0)


if __name__ == '__main__':
    run()
