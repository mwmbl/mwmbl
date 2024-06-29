"""
Generate a learn-to-rank dataset.
"""
import sys

import pandas as pd
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.paths import RANKINGS_DATASET_TRAIN_PATH, LEARNING_TO_RANK_DATASET_PATH


def run():
    index_path = sys.argv[1]

    completer = DummyCompleter()

    dataset = get_dataset(completer, index_path)
    print("Dataset", dataset)
    pd.DataFrame(dataset).to_csv(LEARNING_TO_RANK_DATASET_PATH)


def get_dataset(completer, index_path):
    with TinyIndex(item_factory=Document, index_path=index_path) as tiny_index:
        ranker = HeuristicRanker(tiny_index, completer)
        gold_standard = pd.read_csv(RANKINGS_DATASET_TRAIN_PATH, index_col=0)
        dataset = []
        for query, rankings in gold_standard.groupby('query'):
            print("Query", query)
            gold_standard = dict(zip(rankings['url'].tolist(), rankings.index.tolist()))

            predicted, terms = ranker.get_results(query + ' ')
            if len(predicted) == 0:
                continue

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
                    'score': item.score,
                })
                if in_gold_standard:
                    found_gold = True
            if found_gold:
                dataset += new_items

    return dataset


if __name__ == '__main__':
    run()
