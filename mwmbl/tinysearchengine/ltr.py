"""
Learning to rank predictor
"""
from pandas import DataFrame, Series
from sklearn.base import BaseEstimator, RegressorMixin, TransformerMixin

from mwmbl.tinysearchengine.rank import get_match_features


class ThresholdPredictor(BaseEstimator, RegressorMixin):
    def __init__(self, threshold: float, classifier: BaseEstimator):
        self.threshold = threshold
        self.classifier = classifier

    def fit(self, X, y) -> BaseEstimator:
        y_thresholded = y > self.threshold
        self.classifier.fit(X, y_thresholded)
        return self

    def predict(self, X):
        predictions = self.classifier.predict_proba(X)
        if predictions.shape[1] == 2:
            return predictions[:, 1]
        return predictions


def get_match_features_as_series(item: Series):
    terms = item['query'].lower().split()
    last_match_char, match_length, total_possible_match_length = get_match_features(
        terms, item['title'], item['extract'], True)
    return Series({
        'last_match_char': last_match_char,
        'match_length': match_length,
        'total_possible_match_length': total_possible_match_length,
        'num_terms': len(terms),
    })


class FeatureExtractor(BaseEstimator, TransformerMixin):
    def fit(self, X, y=None):
        return self

    def transform(self, X: DataFrame, y=None):
        return X.apply(get_match_features_as_series, axis=1)


