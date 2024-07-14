"""
Evaluate against queries on a remote search engine.
"""
import pickle
from argparse import ArgumentParser

import requests
from joblib import Memory

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter, MwmblRankingModel
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.rankeval.paths import MODEL_PATH
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.ltr_rank import LTRRanker
from mwmbl.tinysearchengine.rank import Ranker, HeuristicRanker, HeuristicAndWikiRanker

from mwmbl.rankeval.evaluation.evaluate import RankingModel, evaluate


memory = Memory(location="devdata/cache")


@memory.cache
def fetch_results(url: str, query: str):
    results = requests.get(url + "/api/v1/search/?s=" + query).json()
    return results


def run():
    ranker = HeuristicAndWikiRanker(RemoteIndex(), DummyCompleter())

    model = pickle.load(open(MODEL_PATH, 'rb'))
    ranker = LTRRanker(ranker, model, 1000)
    # ranker = HeuristicRanker(RemoteIndex(), DummyCompleter())
    model = MwmblRankingModel(ranker)
    evaluate(model, fraction=0.01, use_test=True)


def single_query(query: str):
    ranker = HeuristicAndWikiRanker(RemoteIndex(), DummyCompleter())
    results = ranker.search(query, [])
    for result in results:
        print(result)


if __name__ == '__main__':
    run()
    # single_query("beethoven - wikipedia")
