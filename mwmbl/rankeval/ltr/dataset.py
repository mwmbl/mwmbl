"""
Generate a learn-to-rank dataset.
"""
import sys

import pandas as pd

from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.indexer import TinyIndex, Document, DocumentState
from mwmbl.tinysearchengine.rank import HeuristicRanker, HeuristicAndWikiRanker

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.paths import RANKINGS_DATASET_TRAIN_PATH, LEARNING_TO_RANK_DATASET_PATH


def run():
    completer = DummyCompleter()

    dataset = get_dataset(completer)
    pd.DataFrame(dataset).to_csv(LEARNING_TO_RANK_DATASET_PATH, errors="replace")


def get_dataset(completer):
    index = RemoteIndex()
    ranker = HeuristicAndWikiRanker(index, completer, return_none_if_no_mwmbl_results=True)
    gold_standard = pd.read_csv(RANKINGS_DATASET_TRAIN_PATH, index_col=0)
    dataset = []
    for query, rankings in gold_standard.groupby('query'):
        print("Query", query)
        gold_standard = dict(zip(rankings['url'].tolist(), rankings.index.tolist()))

        predicted = ranker.search(query + ' ', [])

        if len(predicted) == 0:
            continue

        print("Found results", len(predicted))

        new_items = []
        found_gold = False
        for item in predicted:
            in_gold_standard = item.url in gold_standard
            new_items.append({
                'gold_standard_rank': gold_standard.get(item.url),
                'query': query,
                'url': item.url,
                'title': item.title,
                'extract': item.extract,
                'state': item.state,
                'score': item.score,
            })
            if in_gold_standard:
                found_gold = True
        if found_gold:
            dataset += new_items

    return dataset


if __name__ == '__main__':
    run()
