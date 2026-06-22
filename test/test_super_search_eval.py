"""Tests for the offline evaluation harness on synthetic reward matrices."""
import numpy as np
import pytest

from mwmbl.tinysearchengine.super_search_select import evaluation
from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix
from mwmbl.tinysearchengine.super_search_select.features import FEATURE_NAMES


def _synthetic_matrix(Q=120, S=30, seed=0) -> RewardMatrix:
    """Reward driven mostly by cos_bow (idx 1) and popularity (idx 3)."""
    rng = np.random.default_rng(seed)
    F = len(FEATURE_NAMES)
    X = rng.random((Q, S, F)).astype(np.float64)
    X[:, :, 0] = 1.0  # bias
    cos_i = FEATURE_NAMES.index("cos_bow")
    pop_i = FEATURE_NAMES.index("popularity")
    R = np.clip(0.8 * X[:, :, cos_i] + 0.2 * X[:, :, pop_i]
                + 0.02 * rng.standard_normal((Q, S)), 0.0, 1.0)
    mask = np.ones((Q, S), dtype=bool)
    return RewardMatrix(
        queries=[f"q{i}" for i in range(Q)],
        sources=[f"s{i}" for i in range(S)],
        feature_names=list(FEATURE_NAMES), X=X, R=R, mask=mask,
    )


def test_coverage_bounds_and_oracle():
    m = _synthetic_matrix()
    # Oracle scoring (use true reward as the score) gives coverage 1.0.
    cov = evaluation.coverage_at_k(m.R, m.R, m.mask, k=10)
    assert cov == pytest.approx(1.0)
    # Random scoring is below oracle.
    rng = np.random.default_rng(1)
    rand_cov = evaluation.coverage_at_k(rng.random(m.R.shape), m.R, m.mask, k=10)
    assert 0.0 <= rand_cov < 1.0


def test_baselines_ordering():
    m = _synthetic_matrix()
    out = evaluation.simulate_baselines(m, k=10)
    assert out["oracle"] >= out["cosine"] >= out["random"]
    assert out["cosine"] > out["random"]  # cos_bow is the dominant signal


def test_ts_tuning_beats_random_and_exploration_helps():
    # The harness's job is to pick the exploration scale: a tuned TS should beat
    # random, exploration (nu>0) should beat pure-greedy (nu=0), and nothing
    # beats the oracle.
    m = _synthetic_matrix()
    base = evaluation.simulate_baselines(m, k=10)
    sweep = evaluation.sweep_explore_scale(m, k=10, nus=[0.0, 0.25, 0.5, 1.0, 2.0])
    best_nu = max(sweep, key=sweep.get)
    best = sweep[best_nu]
    assert best > base["random"]
    assert best > sweep[0.0]          # exploration helps vs greedy
    assert best <= base["oracle"] + 1e-9


def test_sweep_explore_scale_runs():
    m = _synthetic_matrix(Q=60, S=20)
    res = evaluation.sweep_explore_scale(m, k=8, nus=[0.0, 0.5, 1.0, 2.0])
    assert set(res) == {0.0, 0.5, 1.0, 2.0}
    assert all(v >= 0 for v in res.values())


def test_save_load_roundtrip(tmp_path):
    m = _synthetic_matrix(Q=10, S=5)
    path = tmp_path / "matrix"
    m.save(path)
    loaded = RewardMatrix.load(path)
    assert loaded.sources == m.sources
    assert np.allclose(loaded.R, m.R)
    assert loaded.feature_names == m.feature_names


@pytest.mark.slow
def test_feature_selection_flags_cos_bow(tmp_path):
    pytest.importorskip("xgboost")
    m = _synthetic_matrix(Q=80, S=20)
    result = evaluation.select_features(m, k=8)
    drops = result["ablation_drop"]
    # cos_bow is the dominant driver, so removing it should hurt coverage most.
    assert drops["cos_bow"] == max(drops.values())
