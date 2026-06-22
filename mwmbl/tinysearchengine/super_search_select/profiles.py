"""Per-site content profiles and query-vector caching in Redis.

A *content profile* is a decaying mean of the projected vectors of the results a
site returns; it summarises "what this site tends to surface" and is the
site-side half of the query-vs-site cosine feature. It is updated every time the
site is queried (``update_profile``), so it sharpens as the site is used more.

Vectors are stored as raw float32 bytes, so this module uses its own
binary-safe Redis connection (the shared client in ``search_setup`` /
``super_search`` uses ``decode_responses=True``, which would corrupt bytes).
"""
from __future__ import annotations

import numpy as np
import redis
from django.conf import settings

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.super_search_select import vectors

_PROFILE_BOW = "ss:profile:bow:{site}"
_PROFILE_CNG = "ss:profile:cng:{site}"
_QVEC_BOW = "ss:qvec:bow:{key}"
_QVEC_CNG = "ss:qvec:cng:{key}"

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    """Binary-safe Redis connection (decode_responses defaults to False)."""
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL)
    return _redis


def _dim() -> int:
    return settings.SUPER_SEARCH_PROJECTION_DIM


def sample_text(docs: list[Document]) -> str:
    """Concatenate titles + extracts of a result sample into one text blob."""
    return " ".join(f"{d.title or ''} {d.extract or ''}" for d in docs)


def _blend(old: np.ndarray | None, sample: np.ndarray, decay: float) -> np.ndarray:
    if old is None:
        return sample
    return vectors._l2_normalise((1.0 - decay) * old + decay * sample)


def get_profile(site: str) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Return ``(bow_profile, cng_profile)`` for a site, or ``(None, None)`` if unseen."""
    r = _get_redis()
    bow = vectors.from_bytes(r.get(_PROFILE_BOW.format(site=site)))
    cng = vectors.from_bytes(r.get(_PROFILE_CNG.format(site=site)))
    return bow, cng


def get_profiles(sites: list[str]) -> dict[str, tuple[np.ndarray | None, np.ndarray | None]]:
    """Batch fetch ``(bow, cng)`` profiles for many sites in a single round-trip."""
    if not sites:
        return {}
    r = _get_redis()
    keys = [_PROFILE_BOW.format(site=s) for s in sites] + [_PROFILE_CNG.format(site=s) for s in sites]
    raw = r.mget(keys)
    n = len(sites)
    return {
        site: (vectors.from_bytes(raw[i]), vectors.from_bytes(raw[n + i]))
        for i, site in enumerate(sites)
    }


def update_profile(site: str, docs: list[Document]) -> None:
    """Fold a result sample into the site's decaying-mean content profile."""
    if not docs:
        return
    text = sample_text(docs)
    dim, decay = _dim(), settings.SUPER_SEARCH_PROFILE_DECAY
    bow_old, cng_old = get_profile(site)
    bow_new = _blend(bow_old, vectors.project_bow(text, dim), decay)
    cng_new = _blend(cng_old, vectors.project_char_ngrams(text, dim), decay)
    pipe = _get_redis().pipeline()
    pipe.set(_PROFILE_BOW.format(site=site), vectors.to_bytes(bow_new))
    pipe.set(_PROFILE_CNG.format(site=site), vectors.to_bytes(cng_new))
    pipe.execute()


def get_query_vectors(query: str) -> tuple[np.ndarray, np.ndarray]:
    """Projected ``(bow, cng)`` vectors for a query, cached in Redis with a TTL."""
    key = vectors.query_cache_key(query)
    dim = _dim()
    r = _get_redis()
    bow = vectors.from_bytes(r.get(_QVEC_BOW.format(key=key)))
    cng = vectors.from_bytes(r.get(_QVEC_CNG.format(key=key)))
    if bow is not None and cng is not None:
        return bow, cng
    bow = vectors.project_bow(query, dim)
    cng = vectors.project_char_ngrams(query, dim)
    ttl = settings.SUPER_SEARCH_QVEC_CACHE_TTL
    pipe = r.pipeline()
    pipe.set(_QVEC_BOW.format(key=key), vectors.to_bytes(bow), ex=ttl)
    pipe.set(_QVEC_CNG.format(key=key), vectors.to_bytes(cng), ex=ttl)
    pipe.execute()
    return bow, cng
