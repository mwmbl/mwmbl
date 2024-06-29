"""
Perform an evaluation using NDCG against a gold standard set of results.
"""
from abc import ABC, abstractmethod

import numpy as np
import pandas as pd
from scipy.stats import sem
from sklearn.metrics import ndcg_score


# Sourced from https://www.searchenginejournal.com/google-first-page-clicks/374516/
from mwmbl.rankeval.paths import RANKINGS_DATASET_TEST_PATH

CLICK_PROPORTIONS = [0.285, 0.157, 0.110, 0.080, 0.072, 0.051, 0.040, 0.032, 0.028, 0.025]
NUM_RESULTS_FOR_EVAL = len(CLICK_PROPORTIONS)


random = np.random.default_rng(42)


class RankingModel(ABC):
    @abstractmethod
    def predict(self, query: str) -> list[str]:
        """
        Generate a list of URLs as search results for the given query.
        """
        pass


def evaluate(ranking_model: RankingModel, fraction: float = 1.0):
    # TODO:
    #  - output feature importances from XGBoost
    #  - experiment with more features

    dataset = pd.read_csv(RANKINGS_DATASET_TEST_PATH)
    ndcg_scores = []
    proportions = []

    queries = dataset['query'].unique()
    if fraction < 1.0:
        num_queries = int(fraction * len(queries))
        print("Num queries", num_queries)
        random_queries = set(random.choice(queries, num_queries, replace=False))
    else:
        random_queries = set(queries)

    for query, rankings in dataset.groupby('query'):
        if query not in random_queries:
            continue

        top_ranked = rankings[['url']].iloc[:NUM_RESULTS_FOR_EVAL]
        top_ranked['score'] = CLICK_PROPORTIONS[:len(top_ranked)]
        scores = top_ranked.set_index('url')['score'].to_dict()
        print(f"Query: '{query}'", scores)

        predicted_urls = ranking_model.predict(query)
        print("Predicted", predicted_urls)
        top_urls = predicted_urls[:NUM_RESULTS_FOR_EVAL]
        y_true = [scores.get(url, 0.0) for url in top_urls] + [0.0] * (10 - len(top_urls))
        y_predicted = list(range(NUM_RESULTS_FOR_EVAL, 0, -1))

        print("Y true", y_true)
        print("Y predicted", y_predicted)

        proportion_matched = len(set(top_urls) & scores.keys()) / NUM_RESULTS_FOR_EVAL
        proportions.append(proportion_matched)

        score = ndcg_score([y_true], [y_predicted])
        ndcg_scores.append(score)

    print("ndcg_score_mean: ", np.mean(ndcg_scores))
    print("ndcg_score_sem:", sem(ndcg_scores))
    print()
    print("proportion_score_mean:", np.mean(proportions))
    print("proportion_score_sem:", sem(proportions))
