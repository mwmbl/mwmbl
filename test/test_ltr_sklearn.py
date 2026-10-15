"""
The scikit-learn building blocks the ranking experiments compose: feature extraction from
result records and a regressor that learns a thresholded label.
"""

import numpy as np
import pytest
from pandas import DataFrame, Series
from sklearn.pipeline import make_pipeline
from sklearn.tree import DecisionTreeClassifier

from mwmbl.tinysearchengine.ltr import FeatureExtractor, ThresholdPredictor, get_features_as_series


def _results() -> DataFrame:
    return DataFrame(
        {
            "query": ["python tutorial", "python tutorial", "cheap flights", "cheap flights"],
            "title": ["Python Tutorial for beginners", "Gardening tips", "Cheap flights to Spain", "Python Tutorial"],
            "url": [
                "https://docs.python.org/tutorial",
                "https://garden.example/tips",
                "https://flights.example/spain",
                "https://docs.python.org/tutorial",
            ],
            "extract": ["Learn python step by step", "Grow tomatoes", "Find cheap flights", "Learn python"],
            "score": [10.0, 1.0, 9.0, 1.0],
        }
    )


def test_feature_extractor_returns_one_float_row_per_result():
    results = _results()

    features = FeatureExtractor().fit(results).transform(results)

    assert features.shape[0] == len(results)
    assert features.dtype == np.float32
    assert np.isfinite(features).all()


def test_feature_extractor_distinguishes_matching_from_unrelated_results():
    results = _results()

    features = FeatureExtractor().fit_transform(results)

    assert not np.array_equal(features[0], features[1])


def test_features_as_series_names_each_feature():
    features = get_features_as_series(_results().iloc[0])

    assert isinstance(features, Series)
    assert "match_term_proportion_whole" in features.index


def test_threshold_predictor_learns_the_binarised_label():
    results = _results()
    labels = np.array([3.0, 0.0, 3.0, 0.0])
    model = make_pipeline(FeatureExtractor(), ThresholdPredictor(0.0, DecisionTreeClassifier(random_state=0)))

    model.fit(results, labels)
    predictions = model.predict(results)

    assert predictions.tolist() == [1.0, 0.0, 1.0, 0.0]


def test_threshold_predictor_returns_the_whole_matrix_when_only_one_class_was_seen():
    results = _results()
    model = make_pipeline(FeatureExtractor(), ThresholdPredictor(10.0, DecisionTreeClassifier(random_state=0)))

    model.fit(results, np.zeros(len(results)))
    predictions = model.predict(results)

    assert predictions.shape == (len(results), 1)


@pytest.mark.parametrize("threshold, expected", [(0.0, [True, False]), (5.0, [True, False])])
def test_threshold_predictor_binarises_labels_before_fitting(threshold, expected):
    seen = []

    class Recorder(DecisionTreeClassifier):
        def fit(self, X, y, *args, **kwargs):
            seen.append(list(y))
            return super().fit(X, y, *args, **kwargs)

    ThresholdPredictor(threshold, Recorder()).fit(np.array([[1.0], [0.0]]), np.array([6.0, 0.0]))

    assert seen == [expected]
