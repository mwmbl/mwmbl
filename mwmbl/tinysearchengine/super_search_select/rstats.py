"""Online per-source reward statistics in Redis.

Keeps a decaying mean (EMA) of each source's per-request reward — the judge
score its results earn — and serves it back as the ``contribution_ema``
feature. This is the per-arm running mean that a per-request-updated bandit
(LinTS) learns through its intercept; exposing it as a *feature* gives the
batch-trained xgb model the same online signal without per-request model
updates, and lets its trees interact it with the rest of the context.

Aggregate per-source statistics only — nothing query-derived is stored here.
"""

from __future__ import annotations

import redis
from django.conf import settings

from mwmbl.tinysearchengine.super_search_select.features import SiteStats

_REWARD_EMA = "ss:rstat:reward:{site}"

# Weight of the newest observation. Matches the content-profile decay ethos:
# slow enough to be stable, fast enough to track a source going stale.
DECAY = 0.1

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL)
    return _redis


def get_stats(sites: list[str]) -> dict[str, SiteStats]:
    """Per-source online stats; zeros for sources never rewarded (cold)."""
    r = _get_redis()
    pipe = r.pipeline()
    for site in sites:
        pipe.get(_REWARD_EMA.format(site=site))
    values = pipe.execute()
    return {site: SiteStats(contribution_ema=float(v) if v is not None else 0.0) for site, v in zip(sites, values)}


def update(rewards: dict[str, float]) -> None:
    """Fold one request's per-source rewards into the EMAs."""
    r = _get_redis()
    pipe = r.pipeline()
    keys = [_REWARD_EMA.format(site=site) for site in rewards]
    old = r.mget(keys) if keys else []
    for (site, reward), prev in zip(rewards.items(), old):
        ema = reward if prev is None else (1.0 - DECAY) * float(prev) + DECAY * float(reward)
        pipe.set(_REWARD_EMA.format(site=site), repr(float(ema)))
    pipe.execute()


def seed_stats(means: dict[str, float]) -> int:
    """Seed missing reward EMAs (SETNX — never clobbers live stats).

    Used at startup with the per-source mean judge rewards the bundled
    warm-start model was trained against. Returns the number seeded.
    """
    pipe = _get_redis().pipeline()
    for site, mean in means.items():
        pipe.setnx(_REWARD_EMA.format(site=site), repr(float(mean)))
    results = pipe.execute()
    return sum(1 for ok in results if ok)
