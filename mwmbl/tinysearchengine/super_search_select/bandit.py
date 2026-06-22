"""Per-arm linear-Gaussian Thompson sampling for source selection.

Each source (arm) keeps Bayesian linear-regression sufficient statistics over
the context features: ``A = lambda*I + sum(x x^T)`` and ``b = sum(r x)``. The
posterior over the weight vector is ``N(A^-1 b, sigma^2 A^-1)``; Thompson
sampling draws ``theta ~ N(A^-1 b, nu^2 sigma^2 A^-1)`` and scores a candidate by
``theta . x``. An unseen arm has ``A = lambda*I`` (high posterior variance), so
it is explored automatically — no separate cold-start logic needed.

State lives in Redis as raw float32 blobs (binary-safe connection), updated at
request completion and (separately) persisted to Postgres for durability.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import redis
from django.conf import settings

from mwmbl.tinysearchengine.super_search_select.features import NUM_FEATURES

_KEY_A = "ss:bandit:A:{site}"
_KEY_B = "ss:bandit:b:{site}"
_A_PREFIX = "ss:bandit:A:"

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL)
    return _redis


@dataclass
class ArmState:
    A: np.ndarray   # (d, d) precision matrix
    b: np.ndarray   # (d,) weighted-response vector


def prior_state(d: int = NUM_FEATURES) -> ArmState:
    """Ridge prior: A = lambda*I, b = 0 (posterior mean 0, wide covariance)."""
    lam = settings.SUPER_SEARCH_TS_PRIOR_PRECISION
    return ArmState(A=lam * np.eye(d, dtype=np.float64), b=np.zeros(d, dtype=np.float64))


def _decode_state(raw_a: bytes | None, raw_b: bytes | None, d: int) -> ArmState:
    if not raw_a or not raw_b:
        return prior_state(d)
    A = np.frombuffer(raw_a, dtype=np.float64).reshape(d, d).copy()
    b = np.frombuffer(raw_b, dtype=np.float64).copy()
    return ArmState(A=A, b=b)


def get_states(sites: list[str], d: int = NUM_FEATURES) -> dict[str, ArmState]:
    """Batch-load arm states for many sites (prior for any unseen arm)."""
    if not sites:
        return {}
    r = _get_redis()
    keys = [_KEY_A.format(site=s) for s in sites] + [_KEY_B.format(site=s) for s in sites]
    raw = r.mget(keys)
    n = len(sites)
    return {site: _decode_state(raw[i], raw[n + i], d) for i, site in enumerate(sites)}


def save_states(states: dict[str, ArmState]) -> None:
    pipe = _get_redis().pipeline()
    for site, state in states.items():
        pipe.set(_KEY_A.format(site=site), state.A.astype(np.float64).tobytes())
        pipe.set(_KEY_B.format(site=site), state.b.astype(np.float64).tobytes())
    pipe.execute()


def sample_score(state: ArmState, x: np.ndarray, rng: np.random.Generator) -> float:
    """Thompson sample: draw theta from the posterior and score ``theta . x``."""
    A_inv = np.linalg.inv(state.A)
    mean = A_inv @ state.b
    nu = settings.SUPER_SEARCH_TS_EXPLORE_SCALE
    sigma2 = settings.SUPER_SEARCH_TS_NOISE_VARIANCE
    cov = (nu * nu * sigma2) * A_inv
    # Symmetrise to guard against tiny asymmetries from the inverse.
    cov = 0.5 * (cov + cov.T)
    theta = rng.multivariate_normal(mean, cov)
    return float(theta @ x)


def update_arm(state: ArmState, x: np.ndarray, reward: float) -> ArmState:
    """Rank-1 Bayesian update: A += x x^T, b += reward * x."""
    state.A += np.outer(x, x)
    state.b += reward * x
    return state


def all_sites() -> list[str]:
    """Names of every arm with state in Redis (used by the persistence task)."""
    r = _get_redis()
    sites = []
    for key in r.scan_iter(match=_A_PREFIX + "*"):
        k = key.decode() if isinstance(key, (bytes, bytearray)) else key
        sites.append(k[len(_A_PREFIX):])
    return sites


def export_all() -> dict[str, ArmState]:
    """Load every arm's state (for Redis -> Postgres persistence)."""
    return get_states(all_sites())


def seed_states(states: dict[str, ArmState], overwrite: bool = False) -> None:
    """Write states to Redis. With ``overwrite=False`` only fills missing keys
    (used to restore from Postgres without clobbering newer live state)."""
    pipe = _get_redis().pipeline()
    for site, state in states.items():
        pipe.set(_KEY_A.format(site=site), state.A.astype(np.float64).tobytes(), nx=not overwrite)
        pipe.set(_KEY_B.format(site=site), state.b.astype(np.float64).tobytes(), nx=not overwrite)
    pipe.execute()


def update(features: dict[str, list[float]], rewards: dict[str, float]) -> None:
    """Apply observed rewards to the selected arms and persist the new states.

    ``features`` are the feature vectors used at selection time (so the update is
    consistent with the action), keyed by source.
    """
    sites = [s for s in rewards if s in features]
    if not sites:
        return
    states = get_states(sites)
    for site in sites:
        x = np.asarray(features[site], dtype=np.float64)
        update_arm(states[site], x, rewards[site])
    save_states(states)
