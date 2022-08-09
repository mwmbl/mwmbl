"""
Learning to rank predictor
"""
from pandas import DataFrame, Series
from sklearn.base import BaseEstimator, RegressorMixin, TransformerMixin

from mwmbl.tinysearchengine.rank import get_features


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


def get_features_as_series(item: Series):
    terms = item['query'].lower().split()
    features = get_features(terms, item['title'], item['url'], item['extract'], item['score'], True)
    # features_filtered = {k: v for k, v in features.items() if 'match_score' not in k}
    return Series(features)


class FeatureExtractor(BaseEstimator, TransformerMixin):
    def fit(self, X, y=None):
        return self

    def transform(self, X: DataFrame, y=None):
        features = X.apply(get_features_as_series, axis=1)
        print("Features", features.columns)
        return features


