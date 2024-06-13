"""
Evaluate the crawled URLs to see how well they cover our gold-standard search results.
"""

import pandas as pd
from pandas import DataFrame

from mwmbl.rankeval.evaluation.evaluate import evaluate, RankingModel, NUM_RESULTS_FOR_EVAL
from mwmbl.rankeval.evaluation.urldb import find_matching_urls
from mwmbl.rankeval.paths import URLS_PATH, RANKINGS_DATASET_TEST_PATH


class OracleRankingModel(RankingModel):
    """
    A ranking model that has access to the gold standard and will return back any matching queries in the order
    they occur in the gold standard
    """

    def __init__(self):
        self.rankings = {}

    def prepare_rankings(self, gold_standard: DataFrame, url_database_path: str):
        for query, rankings in gold_standard.groupby('query'):
            gold_standard_urls = rankings['url'].to_list()[:NUM_RESULTS_FOR_EVAL]
            matches = set(find_matching_urls(url_database_path, gold_standard_urls))
            oracle_ranking = [url for url in gold_standard_urls if url in matches]
            if oracle_ranking:
                print("Oracle ranking", query, oracle_ranking)
            self.rankings[query] = oracle_ranking

    def predict(self, query: str) -> list[str]:
        return self.rankings[query]


def run():
    model = OracleRankingModel()
    gold_standard = pd.read_csv(RANKINGS_DATASET_TEST_PATH)
    model.prepare_rankings(gold_standard, URLS_PATH)
    evaluate(model)


if __name__ == '__main__':
    run()
