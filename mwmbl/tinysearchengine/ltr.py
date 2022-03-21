"""
Learning to rank predictor
"""
from pandas import DataFrame, Series
from sklearn.base import BaseEstimator, RegressorMixin, TransformerMixin

from mwmbl.tinysearchengine.rank import get_match_features, get_domain_score


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
    last_match_char_title, match_length_title, total_possible_match_length_title = get_match_features(
        terms, item['title'], True, False)
    last_match_char_extract, match_length_extract, total_possible_match_length_extract = get_match_features(
        terms, item['extract'], True, False)
    last_match_char_url, match_length_url, total_possible_match_length_url = get_match_features(
        terms, item['title'], True, False)
    domain_score = get_domain_score(item['url'])
    return Series({
        'last_match_char_title': last_match_char_title,
        'match_length_title': match_length_title,
        'total_possible_match_length_title': total_possible_match_length_title,
        'last_match_char_extract': last_match_char_extract,
        'match_length_extract': match_length_extract,
        'total_possible_match_length_extract': total_possible_match_length_extract,
        'last_match_char_url': last_match_char_url,
        'match_length_url': match_length_url,
        'total_possible_match_length_url': total_possible_match_length_url,
        'num_terms': len(terms),
        'domain_score': domain_score,
        'item_score': item['score'],
    })


class FeatureExtractor(BaseEstimator, TransformerMixin):
    def fit(self, X, y=None):
        return self

    def transform(self, X: DataFrame, y=None):
        features = X.apply(get_match_features_as_series, axis=1)
        print("Features", features.columns)
        return features


