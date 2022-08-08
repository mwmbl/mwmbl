"""
Learning to rank predictor
"""
from pandas import DataFrame, Series
from sklearn.base import BaseEstimator, RegressorMixin, TransformerMixin

from mwmbl.tinysearchengine.rank import get_match_features, get_domain_score, score_match


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
    features = {}
    for part in ['title', 'extract', 'url']:
        last_match_char, match_length, total_possible_match_length = get_match_features(terms, item[part], True, False)
        features[f'last_match_char_{part}'] = last_match_char
        features[f'match_length_{part}'] = match_length
        features[f'total_possible_match_length_{part}'] = total_possible_match_length
        # features[f'score_{part}'] = score_match(last_match_char, match_length, total_possible_match_length)

    features['num_terms'] = len(terms)
    features['num_chars'] = len(' '.join(terms))
    features['domain_score'] = get_domain_score(item['url'])
    features['url_length'] = len(item['url'])
    features['item_score'] = item['score']
    return Series(features)


class FeatureExtractor(BaseEstimator, TransformerMixin):
    def fit(self, X, y=None):
        return self

    def transform(self, X: DataFrame, y=None):
        features = X.apply(get_match_features_as_series, axis=1)
        print("Features", features.columns)
        return features


