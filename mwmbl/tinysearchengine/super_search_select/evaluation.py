"""Offline evaluation for source selection: feature selection + policy simulation.

Everything is driven by a dense reward matrix (queries x sources): the feature
vector ``X[q, s]`` each (query, source) pair was/​would be scored on, the implicit
reward ``R[q, s]`` (fraction of that source's results surviving into the final
top-K), and a ``mask`` of which sources actually returned anything.

With every cell filled we can, without any online traffic:
  * select features  — fit an XGBoost reward model (grouped CV by query) and
    rank/ablate features by held-out recall@k;
  * simulate the policy — replay linear-Gaussian Thompson sampling
    (``simulate_ts``) or the epsilon-greedy XGBoost contextual bandit
    (``simulate_xgb``) over the matrix and compare against baselines
    (random / popularity / cosine / oracle);
  * holdout-evaluate the xgb source model (``evaluate_holdout``): split
    queries into train/test (stratified by each query's home source), fit on
    train, and report test coverage@k / home-source recall@k against the same
    baselines plus a train-then-greedy LinTS comparator.

Off-policy note: replay over a *dense* matrix is unbiased (every arm's
counterfactual reward is known). The online impression log, by contrast, is
conditioned on whatever policy was live; the mitigations are warm-starting
from a dense offline matrix and the epsilon slots continuously injecting
randomized pairs.

This module is pure (numpy/sklearn/xgboost only); building the matrix from live
sources lives in ``scripts/super_search_eval.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class RewardMatrix:
    queries: list[str]
    sources: list[str]
    feature_names: list[str]
    X: np.ndarray  # (Q, S, F) features
    R: np.ndarray  # (Q, S) reward in [0, 1]
    mask: np.ndarray  # (Q, S) bool: source returned results for the query

    def save(self, path: str | Path) -> None:
        path = Path(path)
        np.savez_compressed(path.with_suffix(".npz"), X=self.X, R=self.R, mask=self.mask)
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "queries": self.queries,
                    "sources": self.sources,
                    "feature_names": self.feature_names,
                }
            )
        )

    @classmethod
    def load(cls, path: str | Path) -> "RewardMatrix":
        path = Path(path)
        arrs = np.load(path.with_suffix(".npz"))
        meta = json.loads(path.with_suffix(".json").read_text())
        return cls(
            queries=meta["queries"],
            sources=meta["sources"],
            feature_names=meta["feature_names"],
            X=arrs["X"],
            R=arrs["R"],
            mask=arrs["mask"],
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
    return [(q, s) for q in range(matrix.X.shape[0]) for s in range(matrix.X.shape[1]) if matrix.mask[q, s]]


def evaluate_feature_set(
    matrix: RewardMatrix, feature_idx: list[int], k: int = 10, n_splits: int = 5, seed: int = 0
) -> dict:
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
        model = XGBRegressor(n_estimators=200, max_depth=4, learning_rate=0.1, subsample=0.8, random_state=seed)
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


def simulate_ts(
    matrix: RewardMatrix, k: int, nu: float, sigma2: float = 0.25, lam: float = 1.0, seed: int = 0
) -> float:
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


def sweep_explore_scale(
    matrix: RewardMatrix, k: int, nus: list[float], sigma2: float = 0.25, lam: float = 1.0, seed: int = 0
) -> dict[float, float]:
    """Mean captured reward for each candidate exploration scale ``nu``."""
    return {nu: simulate_ts(matrix, k, nu, sigma2, lam, seed) for nu in nus}


def simulate_xgb(
    matrix: RewardMatrix,
    k: int,
    epsilon: float,
    refit_every: int = 200,
    min_rows: int = 300,
    seed: int = 0,
    params: dict | None = None,
) -> float:
    """Replay the epsilon-greedy XGBoost contextual bandit over the matrix.

    Mirrors ``simulate_ts``'s contract (mean per-query captured reward).
    Queries are visited in a seeded shuffled order; until ``min_rows`` chosen
    (source, reward) pairs have accumulated, selection uses the cosine
    baseline (matching how real impression logs bootstrap), after which the
    model is fit and refit every ``refit_every`` queries.

    The per-source reward EMA (the online stat behind ``contribution_ema``,
    kept in Redis in production) is recomputed along the replay and injected
    into that feature slot at decision time, exactly as serving would see it.
    """
    from mwmbl.tinysearchengine.super_search_select import xgb_model
    from mwmbl.tinysearchengine.super_search_select.rstats import DECAY

    rng = np.random.default_rng(seed)
    Q, S, _ = matrix.X.shape
    vocab = xgb_model.build_vocab(matrix.sources)
    vocab_index = {name: i for i, name in enumerate(vocab)}
    cos_i = matrix.feature_names.index("cos_bow")
    ema_i = matrix.feature_names.index("contribution_ema")

    ema = np.zeros(S)
    ema_seen = np.zeros(S, dtype=bool)
    model = None
    xs: list[np.ndarray] = []
    ys: list[float] = []
    captured = 0.0
    since_fit = 0

    def encode(q: int, s: int) -> np.ndarray:
        x = matrix.X[q, s].copy()
        x[ema_i] = ema[s]
        return xgb_model.encode(x, matrix.sources[s], vocab_index)

    for q in rng.permutation(Q):
        avail = np.where(matrix.mask[q])[0]
        if avail.size == 0:
            continue
        k_q = min(k, avail.size)
        if model is None:
            chosen = avail[np.argsort(matrix.X[q, avail, cos_i])[::-1][:k_q]]
        else:
            enc = np.stack([encode(q, s) for s in avail])
            ranked = avail[np.argsort(model.predict(enc))[::-1]]
            n_explore = int(rng.binomial(k_q, epsilon))
            chosen = list(ranked[: k_q - n_explore])
            rest = ranked[k_q - n_explore :]
            if n_explore and rest.size:
                chosen += list(rng.choice(rest, size=min(n_explore, rest.size), replace=False))
            chosen = np.asarray(chosen)
        for s in chosen:
            xs.append(encode(q, s))
            ys.append(float(matrix.R[q, s]))
            captured += matrix.R[q, s]
        for s in chosen:
            r = float(matrix.R[q, s])
            ema[s] = r if not ema_seen[s] else (1.0 - DECAY) * ema[s] + DECAY * r
            ema_seen[s] = True
        since_fit += 1
        if len(ys) >= min_rows and (model is None or since_fit >= refit_every):
            model = xgb_model.train(np.stack(xs), np.asarray(ys), params=params)
            since_fit = 0
    return captured / Q


def sweep_epsilon(
    matrix: RewardMatrix, k: int, epsilons: list[float], refit_every: int = 200, min_rows: int = 300, seed: int = 0
) -> dict[float, float]:
    """Mean captured reward for each candidate exploration rate ``epsilon``."""
    return {eps: simulate_xgb(matrix, k, eps, refit_every, min_rows, seed) for eps in epsilons}


# ---------------------------------------------------------------------------
# Holdout evaluation of the xgb source model
# ---------------------------------------------------------------------------


def _holdout_split(
    matrix: RewardMatrix, home: list[str | None], test_frac: float, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Split query indices into (train, test), stratified by home source so
    every source's home queries appear in both splits where possible."""
    rng = np.random.default_rng(seed)
    by_home: dict[str | None, list[int]] = {}
    for q, h in enumerate(home):
        by_home.setdefault(h, []).append(q)
    test: list[int] = []
    for qs in by_home.values():
        qs = list(rng.permutation(qs))
        n_test = round(len(qs) * test_frac)
        if len(qs) >= 2:
            n_test = max(n_test, 1)
        test.extend(qs[:n_test])
    test_idx = np.array(sorted(test), dtype=int)
    train_idx = np.array(sorted(set(range(len(home))) - set(test)), dtype=int)
    return train_idx, test_idx


def _home_recall_at_k(
    scores: np.ndarray, matrix: RewardMatrix, home: list[str | None], rows: np.ndarray, k: int
) -> float:
    """Fraction of queries whose home source lands in the top-k scored sources."""
    s_index = {name: i for i, name in enumerate(matrix.sources)}
    hits, n = 0, 0
    for q in rows:
        h = home[q]
        if h is None or h not in s_index:
            continue
        avail = np.where(matrix.mask[q])[0]
        if avail.size == 0 or s_index[h] not in avail:
            continue
        top = avail[np.argsort(scores[q, avail])[::-1][:k]]
        hits += int(s_index[h] in top)
        n += 1
    return hits / n if n else 0.0


def _lints_posterior_means(
    matrix: RewardMatrix,
    train_idx: np.ndarray,
    k: int,
    nu: float = 0.05,
    sigma2: float = 0.25,
    lam: float = 1.0,
    seed: int = 0,
) -> np.ndarray:
    """Replay LinTS over the train queries; return per-arm posterior means for
    greedy scoring of held-out queries."""
    rng = np.random.default_rng(seed)
    S, F = matrix.X.shape[1], matrix.X.shape[2]
    A = np.stack([lam * np.eye(F) for _ in range(S)])
    b = np.zeros((S, F))
    for q in train_idx:
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
        for s in avail[np.argsort(scores[avail])[::-1][:k]]:
            x = matrix.X[q, s]
            A[s] += np.outer(x, x)
            b[s] += matrix.R[q, s] * x
    return np.stack([np.linalg.inv(A[s]) @ b[s] for s in range(S)])


def evaluate_holdout(
    matrix: RewardMatrix,
    k: int = 10,
    test_frac: float = 0.2,
    seed: int = 0,
    home_by_query: dict[str, str] | None = None,
    params: dict | None = None,
) -> dict:
    """Train the xgb source model on a query split, evaluate on the rest.

    ``home_by_query`` maps a query to the source it was written for (the
    synthetic per-source query set); if omitted, each query's oracle-best
    source stands in. Returns test coverage@k, RMSE, home-source recall@k,
    and the same metrics for random/popularity/cosine baselines plus a
    train-then-greedy LinTS comparator.
    """
    from mwmbl.tinysearchengine.super_search_select import xgb_model

    if home_by_query is not None:
        home = [home_by_query.get(q) for q in matrix.queries]
    else:
        home = [
            matrix.sources[int(np.argmax(np.where(matrix.mask[q], matrix.R[q], -np.inf)))]
            if matrix.mask[q].any()
            else None
            for q in range(len(matrix.queries))
        ]
    train_idx, test_idx = _holdout_split(matrix, home, test_frac, seed)

    vocab = xgb_model.build_vocab(matrix.sources)
    vocab_index = {name: i for i, name in enumerate(vocab)}

    # Serve-time analog of the per-source reward EMA (rstats): each source's
    # mean train-split reward, injected into the contribution_ema slot for
    # both splits so the model can use the online stat it will see live.
    ema_i = matrix.feature_names.index("contribution_ema")
    train_mask = np.zeros(len(matrix.queries), dtype=bool)
    train_mask[train_idx] = True
    seen = matrix.mask & train_mask[:, None]
    with np.errstate(invalid="ignore"):
        source_mean = np.where(
            seen.sum(axis=0) > 0, (matrix.R * seen).sum(axis=0) / np.maximum(seen.sum(axis=0), 1), 0.0
        )

    def rows_for(idx: np.ndarray):
        xs, ys, cells = [], [], []
        for q in idx:
            for s in np.where(matrix.mask[q])[0]:
                x = matrix.X[q, s].copy()
                x[ema_i] = source_mean[s]
                xs.append(xgb_model.encode(x, matrix.sources[s], vocab_index))
                ys.append(float(matrix.R[q, s]))
                cells.append((q, s))
        return np.stack(xs), np.asarray(ys), cells

    X_train, y_train, _ = rows_for(train_idx)
    X_test, y_test, test_cells = rows_for(test_idx)
    model = xgb_model.train(X_train, y_train, params=params)
    preds = model.predict(X_test)

    scores = np.full(matrix.R.shape, -np.inf)
    for (q, s), p in zip(test_cells, preds):
        scores[q, s] = p

    R_t, m_t = matrix.R[test_idx], matrix.mask[test_idx]

    def coverage(score_grid: np.ndarray) -> float:
        return coverage_at_k(score_grid[test_idx], R_t, m_t, k)

    rng = np.random.default_rng(seed)
    rand_scores = rng.random(matrix.R.shape)
    pop_i = matrix.feature_names.index("popularity")
    cos_i = matrix.feature_names.index("cos_bow")
    theta = _lints_posterior_means(matrix, train_idx, k, seed=seed)
    lints_scores = np.einsum("qsf,sf->qs", matrix.X, theta)

    out = {
        "n_train_queries": int(train_idx.size),
        "n_test_queries": int(test_idx.size),
        "rmse": float(np.sqrt(np.mean((preds - y_test) ** 2))),
        "coverage_at_k": {
            "xgb": coverage(scores),
            "lints_greedy": coverage(lints_scores),
            "cosine": coverage(matrix.X[:, :, cos_i]),
            "popularity": coverage(matrix.X[:, :, pop_i]),
            "random": coverage(rand_scores),
        },
        "home_recall_at_k": {
            "xgb": _home_recall_at_k(scores, matrix, home, test_idx, k),
            "lints_greedy": _home_recall_at_k(lints_scores, matrix, home, test_idx, k),
            "cosine": _home_recall_at_k(matrix.X[:, :, cos_i], matrix, home, test_idx, k),
            "popularity": _home_recall_at_k(matrix.X[:, :, pop_i], matrix, home, test_idx, k),
            "random": _home_recall_at_k(rand_scores, matrix, home, test_idx, k),
        },
    }
    return out
