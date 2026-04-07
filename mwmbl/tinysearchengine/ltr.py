"""
Learning to rank predictors.

Contains:
- FeatureExtractor: sklearn transformer that calls the Python get_features
- ThresholdPredictor: sklearn wrapper that binarises labels before training
- RankingPredictor: sklearn wrapper with term-count filtering
- RustXGBPipeline: thin Python shim over the Rust mwmbl_rank.RustXGBPipeline,
  providing a sklearn-compatible interface (fit / predict / save_model / load_model).
"""
from pathlib import Path

import mwmbl_rank
import numpy as np
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
    return Series(features)


class FeatureExtractor(BaseEstimator, TransformerMixin):
    def fit(self, X, y=None):
        return self

    def transform(self, X: DataFrame, y=None):
        records = X.to_dict('records')
        all_features = []

        for item in records:
            terms = item['query'].lower().split()

            features = get_features(
                terms, item['title'], item['url'],
                item['extract'], item['score'], True
            )
            all_features.append(features)

        return DataFrame(all_features).values.astype('float32')


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


class RustXGBPipeline(BaseEstimator, RegressorMixin):
    """
    Sklearn-compatible wrapper around the Rust mwmbl_rank.RustXGBPipeline.

    All feature extraction and XGBoost training/inference runs in Rust.
    This class handles the DataFrame → list[dict] conversion at the boundary.

    Parameters
    ----------
    threshold : float
        Label binarisation threshold (default 0.0). Labels > threshold → 1, else 0.
    scale_pos_weight : float
        XGBoost scale_pos_weight hyperparameter (default 0.1).
    reg_lambda : float
        XGBoost reg_lambda hyperparameter (default 2.0).
    num_rounds : int
        Number of XGBoost boosting rounds (default 100).
    """

    def __init__(
        self,
        threshold: float = 0.0,
        scale_pos_weight: float = 0.1,
        reg_lambda: float = 2.0,
        num_rounds: int = 100,
    ):
        self.threshold = threshold
        self.scale_pos_weight = scale_pos_weight
        self.reg_lambda = reg_lambda
        self.num_rounds = num_rounds
        self._inner = mwmbl_rank.RustXGBPipeline(
            threshold=self.threshold,
            scale_pos_weight=self.scale_pos_weight,
            reg_lambda=self.reg_lambda,
            num_rounds=self.num_rounds,
        )

    @staticmethod
    def _df_to_records(X: DataFrame) -> list:
        """Convert a DataFrame to a list of dicts for the Rust boundary."""
        cols = ['query', 'url', 'title', 'extract', 'score']
        subset = X[cols].copy()
        subset['title'] = subset['title'].fillna('')
        subset['extract'] = subset['extract'].fillna('')
        subset['score'] = subset['score'].fillna(0.0)
        return subset.to_dict('records')

    def fit(self, X: DataFrame, y) -> 'RustXGBPipeline':
        """
        Train the XGBoost model.

        Parameters
        ----------
        X : DataFrame with columns query, url, title, extract, score
        y : array-like of float relevance labels
        """
        import time
        t0 = time.time()
        print(f"[RustXGBPipeline.fit] Converting {len(X)} rows to records...", flush=True)
        records = self._df_to_records(X)
        print(f"[RustXGBPipeline.fit] Conversion done in {time.time() - t0:.2f}s. Converting labels...", flush=True)
        t1 = time.time()
        labels = list(np.asarray(y, dtype=np.float32))
        print(f"[RustXGBPipeline.fit] Labels ready in {time.time() - t1:.2f}s. Getting inner pipeline...", flush=True)
        t2 = time.time()
        print(f"[RustXGBPipeline.fit] Inner pipeline ready in {time.time() - t2:.2f}s. Calling Rust fit()...", flush=True)
        t3 = time.time()
        self._inner.fit(records, labels)
        print(f"[RustXGBPipeline.fit] Rust fit() completed in {time.time() - t3:.2f}s (total: {time.time() - t0:.2f}s).", flush=True)
        return self

    def predict(self, X: DataFrame) -> np.ndarray:
        """
        Predict class-1 probabilities.

        Parameters
        ----------
        X : DataFrame with columns query, url, title, extract, score

        Returns
        -------
        np.ndarray of float32 probabilities in [0, 1], shape (n_samples,)
        """
        records = self._df_to_records(X)
        return np.array(self._inner.predict(records), dtype=np.float32)

    def save_model(self, path: str) -> None:
        """Save the trained model to disk (XGBoost binary format)."""
        self._inner.save_model(path)

    def load_model(self, path: str) -> 'RustXGBPipeline':
        """Load a model from disk (XGBoost binary format)."""
        self._inner.load_model(path)
        return self

    @classmethod
    def from_model_path(
        cls,
        path: str,
        threshold: float = 0.0,
        scale_pos_weight: float = 0.1,
        reg_lambda: float = 2.0,
        num_rounds: int = 100,
    ) -> 'RustXGBPipeline':
        """Load a pre-trained model from disk and return a ready-to-predict pipeline."""
        pipeline = cls(
            threshold=threshold,
            scale_pos_weight=scale_pos_weight,
            reg_lambda=reg_lambda,
            num_rounds=num_rounds,
        )
        pipeline.load_model(path)
        return pipeline

    def __repr__(self) -> str:
        return (
            f"RustXGBPipeline(threshold={self.threshold}, "
            f"scale_pos_weight={self.scale_pos_weight}, "
            f"reg_lambda={self.reg_lambda}, "
            f"num_rounds={self.num_rounds})"
        )
