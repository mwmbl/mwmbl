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
        return features


class RankingPredictor(BaseEstimator, RegressorMixin):
    def __init__(self, feature_extractor: FeatureExtractor, model: BaseEstimator):
        self.feature_extractor = feature_extractor
        self.model = model

    def fit(self, X, y) -> BaseEstimator:
        features = self.feature_extractor.fit_transform(X)
        self.model.fit(features, y)
        return self

    def predict(self, X):
        features = self.feature_extractor.transform(X)
        predictions = self.model.predict(features)
        too_few_match_terms = (features["match_term_proportion_whole"] <= 0.5) & (features["match_terms_whole"] <= 1.0)
        predictions[too_few_match_terms] = 0.0
        return predictions
