"""
Integration tests for the Rust XGBoost pipeline (RustXGBPipeline).

These tests require the mwmbl_rank Rust extension to be built:
    maturin develop

Run with:
    pytest test/test_rust_pipeline.py -v
"""
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

# Skip the entire module if the Rust extension is not built
pytest.importorskip("mwmbl_rank", reason="mwmbl_rank Rust extension not built")

from mwmbl.tinysearchengine.ltr import RustXGBPipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_dataframe(n: int = 30, seed: int = 42) -> pd.DataFrame:
    """Create a synthetic DataFrame with the columns expected by RustXGBPipeline."""
    rng = np.random.RandomState(seed)
    queries = ["rust programming", "python web", "machine learning", "search engine", "open source"]
    titles = [
        "Rust Programming Language",
        "Python Web Framework",
        "Machine Learning Guide",
        "Search Engine Optimization",
        "Open Source Software",
    ]
    urls = [
        "https://www.rust-lang.org/",
        "https://www.djangoproject.com/",
        "https://scikit-learn.org/",
        "https://mwmbl.org/",
        "https://github.com/",
    ]
    extracts = [
        "A systems programming language focused on safety.",
        "The web framework for perfectionists with deadlines.",
        "Machine learning in Python.",
        "A free, open-source search engine.",
        "Where the world builds software.",
    ]

    rows = []
    for i in range(n):
        idx = i % len(queries)
        rows.append({
            "query": queries[idx],
            "url": urls[idx],
            "title": titles[idx],
            "extract": extracts[idx],
            "score": float(rng.uniform(0, 2)),
        })
    return pd.DataFrame(rows)


def make_labels(n: int = 30, seed: int = 42) -> np.ndarray:
    rng = np.random.RandomState(seed)
    return rng.uniform(0, 1, size=n).astype(np.float32)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRustXGBPipelineBasic:
    def test_instantiation(self):
        pipeline = RustXGBPipeline()
        assert pipeline.threshold == 0.0
        assert pipeline.scale_pos_weight == 0.1
        assert pipeline.reg_lambda == 2.0
        assert pipeline.num_rounds == 100

    def test_instantiation_custom_params(self):
        pipeline = RustXGBPipeline(threshold=0.5, scale_pos_weight=0.2, reg_lambda=1.0, num_rounds=50)
        assert pipeline.threshold == 0.5
        assert pipeline.scale_pos_weight == 0.2
        assert pipeline.reg_lambda == 1.0
        assert pipeline.num_rounds == 50

    def test_repr(self):
        pipeline = RustXGBPipeline()
        r = repr(pipeline)
        assert "RustXGBPipeline" in r
        assert "threshold" in r

    def test_predict_without_fit_raises(self):
        pipeline = RustXGBPipeline()
        X = make_dataframe(5)
        with pytest.raises(Exception, match="fit"):
            pipeline.predict(X)


class TestRustXGBPipelineFitPredict:
    @pytest.fixture
    def trained_pipeline(self):
        X = make_dataframe(40)
        y = make_labels(40)
        pipeline = RustXGBPipeline(num_rounds=20)
        pipeline.fit(X, y)
        return pipeline, X, y

    def test_fit_returns_self(self):
        X = make_dataframe(20)
        y = make_labels(20)
        pipeline = RustXGBPipeline(num_rounds=10)
        result = pipeline.fit(X, y)
        assert result is pipeline

    def test_predict_shape(self, trained_pipeline):
        pipeline, X, _ = trained_pipeline
        preds = pipeline.predict(X)
        assert preds.shape == (len(X),)

    def test_predict_dtype(self, trained_pipeline):
        pipeline, X, _ = trained_pipeline
        preds = pipeline.predict(X)
        assert preds.dtype == np.float32

    def test_predict_range(self, trained_pipeline):
        pipeline, X, _ = trained_pipeline
        preds = pipeline.predict(X)
        assert np.all(preds >= 0.0), f"Min prediction: {preds.min()}"
        assert np.all(preds <= 1.0), f"Max prediction: {preds.max()}"

    def test_predict_no_nan(self, trained_pipeline):
        pipeline, X, _ = trained_pipeline
        preds = pipeline.predict(X)
        assert not np.any(np.isnan(preds)), "Predictions contain NaN"

    def test_predict_empty_dataframe(self, trained_pipeline):
        pipeline, X, _ = trained_pipeline
        empty = X.iloc[:0]
        preds = pipeline.predict(empty)
        assert len(preds) == 0

    def test_predict_single_row(self, trained_pipeline):
        pipeline, X, _ = trained_pipeline
        single = X.iloc[:1]
        preds = pipeline.predict(single)
        assert preds.shape == (1,)

    def test_predict_with_null_title(self, trained_pipeline):
        pipeline, X, _ = trained_pipeline
        X_null = X.copy()
        X_null.loc[0, 'title'] = None
        preds = pipeline.predict(X_null)
        assert not np.any(np.isnan(preds))

    def test_predict_with_null_extract(self, trained_pipeline):
        pipeline, X, _ = trained_pipeline
        X_null = X.copy()
        X_null.loc[0, 'extract'] = None
        preds = pipeline.predict(X_null)
        assert not np.any(np.isnan(preds))


class TestRustXGBPipelinePersistence:
    def test_save_and_load(self):
        X = make_dataframe(30)
        y = make_labels(30)
        pipeline = RustXGBPipeline(num_rounds=15)
        pipeline.fit(X, y)
        original_preds = pipeline.predict(X)

        with tempfile.NamedTemporaryFile(suffix=".xgb", delete=False) as f:
            model_path = f.name

        try:
            pipeline.save_model(model_path)
            assert os.path.exists(model_path)
            assert os.path.getsize(model_path) > 0

            # Load into a new pipeline
            loaded = RustXGBPipeline(num_rounds=15)
            loaded.load_model(model_path)
            loaded_preds = loaded.predict(X)

            np.testing.assert_allclose(
                original_preds, loaded_preds, atol=1e-5,
                err_msg="Predictions differ after save/load"
            )
        finally:
            os.unlink(model_path)

    def test_from_model_path(self):
        X = make_dataframe(20)
        y = make_labels(20)
        pipeline = RustXGBPipeline(num_rounds=10)
        pipeline.fit(X, y)
        original_preds = pipeline.predict(X)

        with tempfile.NamedTemporaryFile(suffix=".xgb", delete=False) as f:
            model_path = f.name

        try:
            pipeline.save_model(model_path)
            loaded = RustXGBPipeline.from_model_path(model_path, num_rounds=10)
            loaded_preds = loaded.predict(X)
            np.testing.assert_allclose(original_preds, loaded_preds, atol=1e-5)
        finally:
            os.unlink(model_path)

    def test_load_nonexistent_path_raises(self):
        pipeline = RustXGBPipeline()
        with pytest.raises(Exception):
            pipeline.load_model("/nonexistent/path/model.xgb")


class TestRustXGBPipelineThreshold:
    def test_threshold_binarisation(self):
        """Labels above threshold should be treated as positive class."""
        X = make_dataframe(40)
        # All labels above threshold → all positive → predictions should be high
        y_all_positive = np.ones(40, dtype=np.float32) * 2.0
        pipeline_pos = RustXGBPipeline(threshold=0.0, num_rounds=20)
        pipeline_pos.fit(X, y_all_positive)
        preds_pos = pipeline_pos.predict(X)

        # All labels below threshold → all negative → predictions should be low
        y_all_negative = np.zeros(40, dtype=np.float32) - 1.0
        pipeline_neg = RustXGBPipeline(threshold=0.0, num_rounds=20)
        pipeline_neg.fit(X, y_all_negative)
        preds_neg = pipeline_neg.predict(X)

        assert np.mean(preds_pos) > np.mean(preds_neg), (
            f"Expected positive-class predictions ({np.mean(preds_pos):.3f}) > "
            f"negative-class predictions ({np.mean(preds_neg):.3f})"
        )


class TestRustXGBPipelineNDCG:
    def test_ndcg_above_random(self):
        """After training, NDCG on training data should be above 0.5 (better than random)."""
        from sklearn.metrics import ndcg_score

        # Use the devdata CSV if available, otherwise use synthetic data
        devdata_path = "devdata/rankeval/learning-to-rank.csv"
        if os.path.exists(devdata_path):
            dataset = pd.read_csv(devdata_path, lineterminator='\n')
            dataset['title'] = dataset['title'].fillna('')
            dataset['extract'] = dataset['extract'].fillna('')
            X = dataset[['query', 'url', 'title', 'extract', 'score']]
            y = dataset['gold_standard_rank'].fillna(0).astype(float)
        else:
            # Synthetic: create data where higher score → higher relevance
            X = make_dataframe(50)
            y = X['score'].values.astype(np.float32)

        pipeline = RustXGBPipeline(num_rounds=50)
        pipeline.fit(X, y)
        preds = pipeline.predict(X)

        # Compute NDCG over the whole dataset (single group)
        score = ndcg_score([y.tolist()], [preds.tolist()])
        assert score > 0.5, f"NDCG {score:.3f} is not above 0.5"
