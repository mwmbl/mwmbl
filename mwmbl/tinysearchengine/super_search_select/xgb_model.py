"""XGBoost contextual-bandit model for source selection.

A pointwise ``XGBRegressor`` predicts the judge reward of querying a source
for the current query. Its input is the shared context feature vector
(``features.FEATURE_NAMES``) concatenated with a per-source identity one-hot
block (``src_{name}``): with source identity in the trees, the model can learn
interactions like intent_code x github or cos_bow x arxiv that a within-query
ranker is otherwise structurally blind to. Unknown sources get an all-zero
identity block and back off to the shared features.

There is no per-request update: the model is retrained in batch from
``SuperSearchImpression`` rows (which store per-source shared vectors keyed by
source name, plus judge rewards, and never the query text) or from an offline
``RewardMatrix``. Artifacts are a directory holding ``model.json`` (XGBoost's
native, version-stable format) and ``meta.json`` (source vocabulary, feature
names, provenance); writes are atomic (tmp + ``os.replace``, meta last) so
serving processes can hot-reload safely.

Loading is fail-fast by design: a corrupt artifact, a feature-set mismatch
with the running code, or no artifact anywhere raises a plain exception that
propagates out of source selection. The only quiet path is the designed one —
no artifact in the runtime dir yet (normal before the first online retrain),
in which case the repo-bundled warm-start artifact is used.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from django.conf import settings

from mwmbl.tinysearchengine.super_search_select.features import (
    FEATURE_NAMES,
    NUM_FEATURES,
)

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
MODEL_FILE = "model.json"
META_FILE = "meta.json"
PROFILES_FILE = "profiles.npz"

# Repo-bundled warm-start artifact, committed inside the package so it ships
# with every deployment (unlike devdata, which may not be mounted).
BUNDLED_DIR = Path(__file__).parent / "artifacts" / "xgb"

# How often get_model() re-stats meta.json to pick up a retrain.
RELOAD_CHECK_SECONDS = 60.0

# Tuned offline via evaluation.evaluate_holdout; change only with offline evidence.
XGB_PARAMS = {
    "n_estimators": 200,
    "max_depth": 6,
    "learning_rate": 0.1,
    "subsample": 0.8,
    "random_state": 0,
}


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def build_vocab(names: Iterable[str]) -> list[str]:
    """Frozen, sorted source vocabulary for the identity one-hot block."""
    return sorted(set(names))


def full_feature_names(vocab: Sequence[str]) -> list[str]:
    return list(FEATURE_NAMES) + [f"src_{name}" for name in vocab]


def encode(shared: Sequence[float], source: str, vocab_index: dict[str, int]) -> np.ndarray:
    """shared context vector ++ source identity one-hot.

    Shared vectors shorter than ``NUM_FEATURES`` are zero-padded: features are
    only ever appended to ``FEATURE_NAMES``, so older logged vectors align
    exactly with zeros in the newer slots. A *longer* vector means the data
    came from a newer feature set than this code and is an error.
    """
    v = np.asarray(shared, dtype=np.float64)
    if v.ndim != 1 or v.size > NUM_FEATURES:
        raise ValueError(f"shared feature vector has {v.shape} shape; expected <= {NUM_FEATURES} values")
    x = np.zeros(NUM_FEATURES + len(vocab_index), dtype=np.float64)
    x[: v.size] = v
    idx = vocab_index.get(source)
    if idx is not None:
        x[NUM_FEATURES + idx] = 1.0
    return x


# ---------------------------------------------------------------------------
# Training data
# ---------------------------------------------------------------------------


def build_training_data(
    rows: Iterable[tuple[dict[str, Sequence[float]], dict[str, float]]],
    vocab: Sequence[str],
) -> tuple[np.ndarray, np.ndarray]:
    """(X, y) from impression rows: each row is (features by source, rewards by source).

    One training pair per source with both a stored feature vector and a
    reward — exactly what ``SuperSearchImpression`` logs per request.
    """
    vocab_index = {name: i for i, name in enumerate(vocab)}
    xs, ys = [], []
    for features, rewards in rows:
        for name, reward in rewards.items():
            shared = features.get(name)
            if shared is None:
                continue
            xs.append(encode(shared, name, vocab_index))
            ys.append(float(reward))
    if not xs:
        return (np.zeros((0, NUM_FEATURES + len(vocab))), np.zeros(0))
    return np.stack(xs), np.asarray(ys)


def build_training_data_from_matrix(
    matrix,
) -> tuple[np.ndarray, np.ndarray, list[str], dict[str, float]]:
    """(X, y, vocab, source_reward_means) from a dense offline ``RewardMatrix``.

    Offline matrices carry no online stats, so each source's mean reward over
    its masked cells is injected into the ``contribution_ema`` slot — the
    serve-time analog of the ``rstats`` reward EMA. Without this the model
    would train on a constant-zero column and ignore the live EMA entirely.
    The means are returned so they can ship in the artifact and seed
    ``rstats`` at startup, keeping training and serving consistent.
    """
    if list(matrix.feature_names) != list(FEATURE_NAMES):
        raise ValueError(
            f"matrix feature names {matrix.feature_names} do not match the "
            f"running code's FEATURE_NAMES {list(FEATURE_NAMES)}; rebuild the matrix"
        )
    vocab = build_vocab(matrix.sources)
    vocab_index = {name: i for i, name in enumerate(vocab)}
    ema_i = FEATURE_NAMES.index("contribution_ema")
    counts = matrix.mask.sum(axis=0)
    sums = (matrix.R * matrix.mask).sum(axis=0)
    source_means = {
        matrix.sources[s]: float(sums[s] / counts[s]) if counts[s] else 0.0 for s in range(len(matrix.sources))
    }
    xs, ys = [], []
    for q in range(matrix.X.shape[0]):
        for s in range(matrix.X.shape[1]):
            if matrix.mask[q, s]:
                x = matrix.X[q, s].copy()
                x[ema_i] = source_means[matrix.sources[s]]
                xs.append(encode(x, matrix.sources[s], vocab_index))
                ys.append(float(matrix.R[q, s]))
    return np.stack(xs), np.asarray(ys), vocab, source_means


def train(X: np.ndarray, y: np.ndarray, params: dict | None = None):
    from xgboost import XGBRegressor

    model = XGBRegressor(**(params or XGB_PARAMS))
    model.fit(X, y)
    return model


def train_and_save_from_impressions(
    window_days: int,
    min_rows: int,
    out_dir: str | Path,
) -> dict | None:
    """Batch retrain from logged ``SuperSearchImpression`` rows.

    The impression log stores per-source shared feature vectors (keyed by
    source name — that key is the identity feature) and judge rewards, never
    the query text, so this is the privacy-safe online learning path. Returns
    the saved metrics, or None when fewer than ``min_rows`` (source, reward)
    pairs exist in the window (the designed not-enough-data-yet case; anything
    else that goes wrong raises).
    """
    from datetime import timedelta

    from mwmbl.models import SuperSearchImpression
    from mwmbl.tinysearchengine.super_search_sources import SOURCES

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    rows = list(SuperSearchImpression.objects.filter(timestamp__gte=cutoff).values_list("features", "rewards"))
    n_pairs = sum(len(set(f) & set(r)) for f, r in rows)
    if n_pairs < min_rows:
        logger.info("super-search xgb retrain skipped: %d pairs in %d days (need %d)", n_pairs, window_days, min_rows)
        return None
    names = {name for f, _ in rows for name in f} | set(SOURCES)
    vocab = build_vocab(names)
    X, y = build_training_data(rows, vocab)
    model = train(X, y)
    metrics = {"train_rmse": float(np.sqrt(np.mean((model.predict(X) - y) ** 2)))}
    save_artifact(model, vocab, out_dir, reward_kind="judge", n_rows=len(y), metrics=metrics)
    return metrics


# ---------------------------------------------------------------------------
# Artifact save / load
# ---------------------------------------------------------------------------


def save_artifact(
    model,
    vocab: Sequence[str],
    model_dir: str | Path,
    reward_kind: str,
    n_rows: int,
    metrics: dict | None = None,
    source_reward_means: dict[str, float] | None = None,
) -> None:
    """Atomically write ``model.json`` + ``meta.json`` (meta last: its presence
    and mtime are the load/reload signal)."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    # tmp name keeps the .json extension so xgboost writes the JSON format
    tmp_model = model_dir / ("tmp." + MODEL_FILE)
    model.save_model(tmp_model)
    os.replace(tmp_model, model_dir / MODEL_FILE)

    meta = {
        "format_version": FORMAT_VERSION,
        "shared_feature_names": list(FEATURE_NAMES),
        "source_vocab": list(vocab),
        "reward_kind": reward_kind,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_rows": int(n_rows),
        "metrics": metrics or {},
    }
    if source_reward_means is not None:
        # Seeds the rstats reward EMAs at startup (see seed_online_state).
        meta["source_reward_means"] = {k: round(v, 6) for k, v in source_reward_means.items()}
    tmp_meta = model_dir / (META_FILE + ".tmp")
    tmp_meta.write_text(json.dumps(meta, indent=2))
    os.replace(tmp_meta, model_dir / META_FILE)
    logger.info("saved super-search xgb artifact to %s (%d rows, reward=%s)", model_dir, n_rows, reward_kind)


@dataclass
class XgbSourceModel:
    model: object  # fitted XGBRegressor
    vocab: list[str]
    vocab_index: dict[str, int]
    meta: dict

    def score(self, feats: dict[str, Sequence[float]]) -> dict[str, float]:
        """Predicted reward for each source given its shared feature vector."""
        if not feats:
            return {}
        names = list(feats)
        X = np.stack([encode(feats[n], n, self.vocab_index) for n in names])
        preds = self.model.predict(X)
        return {name: float(p) for name, p in zip(names, preds)}


def load_artifact(model_dir: str | Path) -> XgbSourceModel:
    """Load and validate an artifact directory. Raises on anything unexpected:
    missing files, unreadable JSON, format or feature-set mismatch."""
    from xgboost import XGBRegressor

    model_dir = Path(model_dir)
    meta = json.loads((model_dir / META_FILE).read_text())
    if meta["format_version"] != FORMAT_VERSION:
        raise ValueError(f"artifact {model_dir} has format_version {meta['format_version']}, expected {FORMAT_VERSION}")
    if meta["shared_feature_names"] != list(FEATURE_NAMES):
        raise ValueError(
            f"artifact {model_dir} was trained on features "
            f"{meta['shared_feature_names']} but the running code has "
            f"{list(FEATURE_NAMES)}; retrain the artifact"
        )
    model = XGBRegressor()
    model.load_model(model_dir / MODEL_FILE)
    vocab = list(meta["source_vocab"])
    return XgbSourceModel(
        model=model,
        vocab=vocab,
        vocab_index={name: i for i, name in enumerate(vocab)},
        meta=meta,
    )


def save_profiles(
    profiles: dict[str, tuple[np.ndarray, np.ndarray]],
    model_dir: str | Path,
) -> None:
    """Write the batch content profiles the model was trained against
    (``profiles.npz``: sources + bow/cng matrices) into the artifact dir."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    sources = sorted(profiles)
    tmp = model_dir / ("tmp." + PROFILES_FILE)
    np.savez_compressed(
        tmp,
        sources=np.array(sources),
        bow=np.stack([profiles[s][0] for s in sources]).astype(np.float32),
        cng=np.stack([profiles[s][1] for s in sources]).astype(np.float32),
    )
    # np.savez appends .npz to names without the suffix; tmp already has it.
    os.replace(tmp, model_dir / PROFILES_FILE)


def load_profiles(model_dir: str | Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    arrs = np.load(Path(model_dir) / PROFILES_FILE)
    return {str(site): (arrs["bow"][i], arrs["cng"][i]) for i, site in enumerate(arrs["sources"])}


def seed_online_state() -> dict[str, int]:
    """SETNX-seed Redis with the bundled artifact's online state.

    Content profiles (``profiles.npz``) and per-source reward means
    (``meta.json``) are what the bundled model's ``cos_*`` and
    ``contribution_ema`` features were computed against; seeding them makes
    serving consistent with training from the first request and makes a Redis
    wipe a non-event. Never overwrites live values, so it is safe to run at
    every startup. Raises if the bundled artifact lacks the seed data — the
    bundle is committed with it, so absence is a packaging bug.
    """
    from mwmbl.tinysearchengine.super_search_select import profiles as ss_profiles
    from mwmbl.tinysearchengine.super_search_select import rstats

    seeds = load_profiles(BUNDLED_DIR)
    meta = json.loads((BUNDLED_DIR / META_FILE).read_text())
    means = meta["source_reward_means"]
    seeded = {
        "profiles": ss_profiles.seed_profiles(seeds),
        "reward_emas": rstats.seed_stats(means),
    }
    logger.info("super-search online state seeded: %s", seeded)
    return seeded


# ---------------------------------------------------------------------------
# Serving singleton
# ---------------------------------------------------------------------------

_cached: XgbSourceModel | None = None
_cached_key: tuple[Path, float] | None = None
_next_check: float = 0.0
_lock = threading.Lock()


def _artifact_dir() -> Path:
    """Runtime dir if it holds an artifact (i.e. an online retrain has run),
    else the repo-bundled warm-start artifact."""
    runtime = Path(settings.SUPER_SEARCH_XGB_MODEL_DIR)
    if (runtime / META_FILE).exists():
        return runtime
    return BUNDLED_DIR


def get_model() -> XgbSourceModel:
    """Shared per-process model, hot-reloaded when a retrain replaces the artifact.

    Fail-fast: if no artifact exists anywhere or the artifact is invalid this
    raises (FileNotFoundError / ValueError / json errors) rather than falling
    back — a missing model is a deployment bug to fix, not a state to mask.
    """
    global _cached, _cached_key, _next_check
    now = time.monotonic()
    if _cached is not None and now < _next_check:
        return _cached
    with _lock:
        if _cached is not None and time.monotonic() < _next_check:
            return _cached
        model_dir = _artifact_dir()
        mtime = (model_dir / META_FILE).stat().st_mtime
        key = (model_dir, mtime)
        if key != _cached_key:
            _cached = load_artifact(model_dir)
            _cached_key = key
            logger.info(
                "super-search xgb model loaded from %s (trained %s, %d sources)",
                model_dir,
                _cached.meta.get("trained_at"),
                len(_cached.vocab),
            )
        _next_check = time.monotonic() + RELOAD_CHECK_SECONDS
        return _cached


def reset_model_cache() -> None:
    """Drop the cached model (tests / after an in-process retrain)."""
    global _cached, _cached_key, _next_check
    with _lock:
        _cached = None
        _cached_key = None
        _next_check = 0.0
