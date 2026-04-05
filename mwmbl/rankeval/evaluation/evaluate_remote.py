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
from mwmbl.tinysearchengine.rank import DomainLimitingRanker, Ranker, HeuristicRanker, HeuristicAndWikiRanker

from mwmbl.rankeval.evaluation.evaluate import RankingModel, evaluate


memory = Memory(location="devdata/cache")


@memory.cache
def fetch_results(url: str, query: str):
    results = requests.get(url + "/api/v1/search/?s=" + query).json()
    return results


def run():
    # ranker = HeuristicAndWikiRanker(RemoteIndex(), DummyCompleter())
 
    model = pickle.load(open(MODEL_PATH, 'rb'))
    ranker = DomainLimitingRanker(LTRRanker(RemoteIndex(), DummyCompleter(), model, 1000, True, 3))
    # ranker = HeuristicRanker(RemoteIndex(), DummyCompleter())
    # ranker = HeuristicAndWikiRanker(RemoteIndex(), DummyCompleter(), max_wiki_results=3)
    model = MwmblRankingModel(ranker)
    evaluate(model, fraction=0.1, use_test=False)


def single_query(query: str):
    model = pickle.load(open(MODEL_PATH, 'rb'))
    ranker = DomainLimitingRanker(LTRRanker(RemoteIndex(), DummyCompleter(), model, 1000, True, 3))
    # ranker = HeuristicAndWikiRanker(RemoteIndex(), DummyCompleter())
    results = ranker.search(query, [])
    for result in results:
        print(result)


if __name__ == '__main__':
    run()
    # single_query("oxyphenbutazone")
