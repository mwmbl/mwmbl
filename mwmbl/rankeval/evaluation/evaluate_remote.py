"""
Evaluate against queries on a remote search engine.
"""

from argparse import ArgumentParser

import requests
from joblib import Memory

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter, MwmblRankingModel
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import Ranker, HeuristicRanker

from mwmbl.rankeval.evaluation.evaluate import RankingModel, evaluate


memory = Memory(location="devdata/cache")


@memory.cache
def fetch_results(url: str, query: str):
    results = requests.get(url + "/api/v1/search/?s=" + query).json()
    return results


def run():
    ranker = HeuristicRanker(RemoteIndex(), DummyCompleter())
    model = MwmblRankingModel(ranker)
    evaluate(model, fraction=0.01)


def single_query(query: str):
    ranker = HeuristicRanker(RemoteIndex(), DummyCompleter())
    results = ranker.search(query, [])
    for result in results:
        print(result)


if __name__ == '__main__':
    run()
    # single_query("beethoven - wikipedia")
