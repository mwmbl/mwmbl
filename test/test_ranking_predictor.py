"""
RankingPredictor zeroes pages that match too few query terms, whatever the model says about them.
"""

import numpy as np
from pandas import DataFrame
from sklearn.base import BaseEstimator

from mwmbl.tinysearchengine.ltr import FeatureExtractor, RankingPredictor


class ConstantModel(BaseEstimator):
    def predict(self, features):
        return np.ones(len(features))


def test_pages_with_too_few_matching_terms_are_predicted_zero():
    predictor = RankingPredictor(FeatureExtractor(), ConstantModel())
    items = DataFrame(
        [
            {
                "query": "pasta recipes",
                "title": "Pasta recipes",
                "url": "https://example.com/pasta",
                "extract": "Cooking pasta at home",
                "score": 1.0,
            },
            {
                "query": "zebra",
                "title": "Pasta recipes",
                "url": "https://example.com/pasta",
                "extract": "Cooking pasta at home",
                "score": 1.0,
            },
        ]
    )

    predictions = predictor.predict(items)

    assert predictions[0] == 1.0
    assert predictions[1] == 0.0
