"""Tests for the offline evaluation harness on synthetic reward matrices."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from mwmbl.tinysearchengine.super_search_select import evaluation
from mwmbl.tinysearchengine.super_search_select.domains import (
    host_of,
    registrable,
    source_domain_map,
)
from mwmbl.tinysearchengine.super_search_select.evaluation import RewardMatrix
from mwmbl.tinysearchengine.super_search_select.features import FEATURE_NAMES


def _load_eval_script():
    """Load scripts/super_search_eval.py by path (it isn't an importable package)."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "super_search_eval.py"
    spec = importlib.util.spec_from_file_location("super_search_eval_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _synthetic_matrix(Q=120, S=30, seed=0) -> RewardMatrix:
    """Reward driven mostly by cos_bow (idx 1) and popularity (idx 3)."""
    rng = np.random.default_rng(seed)
    F = len(FEATURE_NAMES)
    X = rng.random((Q, S, F)).astype(np.float64)
    X[:, :, 0] = 1.0  # bias
    cos_i = FEATURE_NAMES.index("cos_bow")
    pop_i = FEATURE_NAMES.index("popularity")
    R = np.clip(0.8 * X[:, :, cos_i] + 0.2 * X[:, :, pop_i] + 0.02 * rng.standard_normal((Q, S)), 0.0, 1.0)
    mask = np.ones((Q, S), dtype=bool)
    return RewardMatrix(
        queries=[f"q{i}" for i in range(Q)],
        sources=[f"s{i}" for i in range(S)],
        feature_names=list(FEATURE_NAMES),
        X=X,
        R=R,
        mask=mask,
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
    assert best > sweep[0.0]  # exploration helps vs greedy
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


# ---------------------------------------------------------------------------
# XGB contextual bandit: replay + holdout evaluation
# ---------------------------------------------------------------------------

FAST_XGB = {"n_estimators": 50, "max_depth": 6, "learning_rate": 0.1, "subsample": 0.8, "random_state": 0}


def _routing_matrix(Q=160, S=16, seed=0):
    """Planted intent -> source routing that only identity x intent explains:
    each source is affine to one intent, each query carries one intent, and the
    reward is high iff they match. Shared features alone can't predict it.
    Returns (matrix, home_by_query) where home is the designated matched source.
    """
    rng = np.random.default_rng(seed)
    F = len(FEATURE_NAMES)
    n_intents = 8
    first_intent = FEATURE_NAMES.index("intent_code")
    X = np.zeros((Q, S, F))
    X[:, :, 0] = 1.0  # bias
    R = np.zeros((Q, S))
    home = {}
    for q in range(Q):
        intent = q % n_intents
        X[q, :, first_intent + intent] = 1.0
        matched = [s for s in range(S) if s % n_intents == intent]
        for s in range(S):
            base = 0.9 if s % n_intents == intent else 0.1
            R[q, s] = np.clip(base + 0.02 * rng.standard_normal(), 0.0, 1.0)
        home[f"q{q}"] = f"s{matched[q % len(matched)]}"
    mask = np.ones((Q, S), dtype=bool)
    matrix = RewardMatrix(
        queries=[f"q{i}" for i in range(Q)],
        sources=[f"s{i}" for i in range(S)],
        feature_names=list(FEATURE_NAMES),
        X=X,
        R=R,
        mask=mask,
    )
    return matrix, home


def test_simulate_xgb_beats_random():
    pytest.importorskip("xgboost")
    m = _synthetic_matrix()
    base = evaluation.simulate_baselines(m, k=10)
    captured = evaluation.simulate_xgb(m, k=10, epsilon=0.1, refit_every=30, min_rows=100, params=FAST_XGB)
    assert captured > base["random"]
    assert captured <= base["oracle"] + 1e-9


def test_evaluate_holdout_learns_identity_intent_routing():
    pytest.importorskip("xgboost")
    m, home = _routing_matrix()
    result = evaluation.evaluate_holdout(m, k=3, test_frac=0.25, home_by_query=home, params=FAST_XGB)
    cov, recall = result["coverage_at_k"], result["home_recall_at_k"]
    # Only identity x intent explains the reward: xgb should crush the
    # identity-blind baselines on both metrics.
    assert cov["xgb"] > cov["random"] + 0.1
    assert cov["xgb"] > cov["popularity"]
    assert recall["xgb"] > recall["random"] + 0.2
    assert result["rmse"] < 0.2


def test_evaluate_holdout_default_home_is_oracle_best():
    pytest.importorskip("xgboost")
    m, _ = _routing_matrix(Q=80, S=8)
    result = evaluation.evaluate_holdout(m, k=3, test_frac=0.25, params=FAST_XGB)
    assert result["n_train_queries"] + result["n_test_queries"] == 80
    assert 0.0 <= result["home_recall_at_k"]["xgb"] <= 1.0


# ---------------------------------------------------------------------------
# Domain matching (shared helpers behind the gold-grounded matrix)
# ---------------------------------------------------------------------------


def test_registrable_folds_subdomains_and_multi_suffixes():
    assert registrable("www.github.com") == "github.com"
    assert registrable("m.example.org") == "example.org"
    assert registrable("news.ycombinator.com") == "ycombinator.com"
    assert registrable("foo.bbc.co.uk") == "bbc.co.uk"  # multi-label suffix kept
    assert registrable("example.com") == "example.com"


def test_host_of_normalises_and_handles_junk():
    assert host_of("https://User@News.YCombinator.com:443/x") == "news.ycombinator.com"
    assert host_of("not a url") == ""


def test_source_domain_map_groups_sources_by_registrable_domain():
    m = source_domain_map()
    assert "github" in m.get("github.com", [])
    assert "hn" in m.get("ycombinator.com", [])  # news.ycombinator.com -> ycombinator.com


# ---------------------------------------------------------------------------
# Gold-grounded matrix construction (build-gold-matrix core logic)
# ---------------------------------------------------------------------------


def test_is_gold_treats_nan_none_blank_as_not_gold():
    mod = _load_eval_script()
    assert mod._is_gold(1) and mod._is_gold("3")
    assert not mod._is_gold(None)
    assert not mod._is_gold(float("nan"))
    assert not mod._is_gold("")
    assert not mod._is_gold("   ")


def test_attribute_rows_binary_gold_and_in_coverage_filter():
    mod = _load_eval_script()
    reg_map = {"github.com": ["github"], "stackoverflow.com": ["stackexchange"]}
    rows = [
        ("q1", "https://github.com/a", "ta", "ea", float("nan")),  # available, not gold
        ("q1", "https://github.com/b", "tb", "eb", 3),  # gold -> github True
        ("q1", "https://stackoverflow.com/x", "tx", "ex", None),  # available, not gold
        ("q2", "https://example.com/none", "t", "e", 1),  # off-source -> dropped
    ]
    per_query, prof_text = mod.attribute_rows(rows, reg_map)

    # q2 had no in-source row, so it's filtered out of the in-coverage set.
    assert set(per_query) == {"q1"}
    # binary has-gold: github saw a gold row, stackexchange only non-gold.
    assert per_query["q1"] == {"github": True, "stackexchange": False}
    # both github rows accumulate into the source's content profile text.
    assert len(prof_text["github"]) == 2
    assert len(prof_text["stackexchange"]) == 1
