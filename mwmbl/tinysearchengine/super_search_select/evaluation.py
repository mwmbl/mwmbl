"""Offline evaluation for source selection: feature selection + policy simulation.

Everything is driven by a dense reward matrix (queries x sources): the feature
vector ``X[q, s]`` each (query, source) pair was/​would be scored on, the implicit
reward ``R[q, s]`` (fraction of that source's results surviving into the final
top-K), and a ``mask`` of which sources actually returned anything.

With every cell filled we can, without any online traffic:
  * select features  — fit an XGBoost reward model (grouped CV by query) and
    rank/ablate features by held-out recall@k;
  * simulate the policy — replay linear-Gaussian Thompson sampling over the
    matrix, sweep the exploration scale ``nu``, and compare against baselines
    (random / popularity / cosine / oracle).

This module is pure (numpy/sklearn/xgboost only); building the matrix from live
sources lives in ``scripts/super_search_eval.py``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from mwmbl.tinysearchengine.super_search_select.features import FEATURE_NAMES


@dataclass
class RewardMatrix:
    queries: list[str]
    sources: list[str]
    feature_names: list[str]
    X: np.ndarray      # (Q, S, F) features
    R: np.ndarray      # (Q, S) reward in [0, 1]
    mask: np.ndarray   # (Q, S) bool: source returned results for the query

    def save(self, path: str | Path) -> None:
        path = Path(path)
        np.savez_compressed(path.with_suffix(".npz"), X=self.X, R=self.R, mask=self.mask)
        path.with_suffix(".json").write_text(json.dumps({
            "queries": self.queries,
            "sources": self.sources,
            "feature_names": self.feature_names,
        }))

    @classmethod
    def load(cls, path: str | Path) -> "RewardMatrix":
        path = Path(path)
        arrs = np.load(path.with_suffix(".npz"))
        meta = json.loads(path.with_suffix(".json").read_text())
        return cls(
            queries=meta["queries"], sources=meta["sources"],
            feature_names=meta["feature_names"],
            X=arrs["X"], R=arrs["R"], mask=arrs["mask"],
        )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def coverage_at_k(scores: np.ndarray, R: np.ndarray, mask: np.ndarray, k: int) -> float:
    """Mean (over queries) reward captured by the top-k scored sources, relative
    to the oracle top-k. 1.0 = picked the best possible k every time."""
    Q = R.shape[0]
    total = 0.0
    n = 0
    for q in range(Q):
        avail = np.where(mask[q])[0]
        if avail.size == 0:
            continue
        oracle = np.sort(R[q, avail])[::-1][:k].sum()
        if oracle <= 0:
            continue
        order = avail[np.argsort(scores[q, avail])[::-1][:k]]
        total += R[q, order].sum() / oracle
        n += 1
    return total / n if n else 0.0


# ---------------------------------------------------------------------------
# Feature selection (XGBoost, grouped CV by query)
# ---------------------------------------------------------------------------

def _flatten(matrix: RewardMatrix, feature_idx: list[int]):
    """Flatten masked (query, source) cells into (X, y, groups) for sklearn."""
    Q, S, _ = matrix.X.shape
    rows, ys, groups = [], [], []
    for q in range(Q):
        for s in range(S):
            if matrix.mask[q, s]:
                rows.append(matrix.X[q, s, feature_idx])
                ys.append(matrix.R[q, s])
                groups.append(q)
    return np.array(rows), np.array(ys), np.array(groups)


def _cell_index(matrix: RewardMatrix) -> list[tuple[int, int]]:
    """The (query, source) cell behind each flattened masked row, in order."""
    return [(q, s)
            for q in range(matrix.X.shape[0])
            for s in range(matrix.X.shape[1])
            if matrix.mask[q, s]]


def evaluate_feature_set(matrix: RewardMatrix, feature_idx: list[int], k: int = 10,
                         n_splits: int = 5, seed: int = 0) -> dict:
    """Grouped CV (by query): train XGBoost on the chosen features, scatter the
    out-of-fold predictions to the (query, source) grid, and report held-out
    coverage@k and RMSE."""
    from sklearn.model_selection import GroupKFold
    from xgboost import XGBRegressor

    X, y, groups = _flatten(matrix, feature_idx)
    cells = _cell_index(matrix)
    pred = np.full(matrix.R.shape, -np.inf)  # unmasked cells stay unselectable
    rmses = []
    gkf = GroupKFold(n_splits=min(n_splits, len(set(groups))))
    for train, test in gkf.split(X, y, groups):
        model = XGBRegressor(n_estimators=200, max_depth=4, learning_rate=0.1,
                             subsample=0.8, random_state=seed)
        model.fit(X[train], y[train])
        p = model.predict(X[test])
        rmses.append(float(np.sqrt(np.mean((p - y[test]) ** 2))))
        for j, i in enumerate(test):
            q, s = cells[i]
            pred[q, s] = p[j]
    return {
        "coverage_at_k": coverage_at_k(pred, matrix.R, matrix.mask, k),
        "rmse": float(np.mean(rmses)),
    }


def select_features(matrix: RewardMatrix, k: int = 10, seed: int = 0) -> dict:
    """Backward ablation: full-set coverage@k plus each feature's drop when removed
    (positive drop => the feature helps held-out selection)."""
    all_idx = list(range(len(matrix.feature_names)))
    base = evaluate_feature_set(matrix, all_idx, k, seed=seed)["coverage_at_k"]
    ablation = {}
    for i, name in enumerate(matrix.feature_names):
        reduced = [j for j in all_idx if j != i]
        if not reduced:
            continue
        cov = evaluate_feature_set(matrix, reduced, k, seed=seed)["coverage_at_k"]
        ablation[name] = base - cov
    return {"baseline_coverage": base, "ablation_drop": ablation}


# ---------------------------------------------------------------------------
# Policy simulation
# ---------------------------------------------------------------------------

def simulate_ts(matrix: RewardMatrix, k: int, nu: float, sigma2: float = 0.25,
                lam: float = 1.0, seed: int = 0) -> float:
    """Replay linear-Gaussian Thompson sampling; return mean per-query captured reward."""
    rng = np.random.default_rng(seed)
    Q, S, F = matrix.X.shape
    A = np.stack([lam * np.eye(F) for _ in range(S)])
    b = np.zeros((S, F))
    captured = 0.0
    for q in range(Q):
        avail = np.where(matrix.mask[q])[0]
        if avail.size == 0:
            continue
        scores = np.full(S, -np.inf)
        for s in avail:
            A_inv = np.linalg.inv(A[s])
            mean = A_inv @ b[s]
            cov = (nu * nu * sigma2) * A_inv
            theta = rng.multivariate_normal(mean, 0.5 * (cov + cov.T))
            scores[s] = theta @ matrix.X[q, s]
        chosen = avail[np.argsort(scores[avail])[::-1][:k]]
        for s in chosen:
            x = matrix.X[q, s]
            A[s] += np.outer(x, x)
            b[s] += matrix.R[q, s] * x
            captured += matrix.R[q, s]
    return captured / Q


def simulate_baselines(matrix: RewardMatrix, k: int, seed: int = 0) -> dict:
    """Mean per-query captured reward for static baselines and the oracle."""
    rng = np.random.default_rng(seed)
    Q = matrix.X.shape[0]
    names = matrix.feature_names
    cos_i = names.index("cos_bow") if "cos_bow" in names else None
    pop_i = names.index("popularity") if "popularity" in names else None

    def run(score_fn) -> float:
        total = 0.0
        for q in range(Q):
            avail = np.where(matrix.mask[q])[0]
            if avail.size == 0:
                continue
            chosen = score_fn(q, avail)[:k]
            total += matrix.R[q, chosen].sum()
        return total / Q

    out = {
        "oracle": run(lambda q, a: a[np.argsort(matrix.R[q, a])[::-1]]),
        "random": run(lambda q, a: rng.permutation(a)),
    }
    if cos_i is not None:
        out["cosine"] = run(lambda q, a: a[np.argsort(matrix.X[q, a, cos_i])[::-1]])
    if pop_i is not None:
        out["popularity"] = run(lambda q, a: a[np.argsort(matrix.X[q, a, pop_i])[::-1]])
    return out


def sweep_explore_scale(matrix: RewardMatrix, k: int, nus: list[float],
                        sigma2: float = 0.25, lam: float = 1.0, seed: int = 0) -> dict[float, float]:
    """Mean captured reward for each candidate exploration scale ``nu``."""
    return {nu: simulate_ts(matrix, k, nu, sigma2, lam, seed) for nu in nus}
