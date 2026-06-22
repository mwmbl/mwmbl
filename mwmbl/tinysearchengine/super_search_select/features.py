"""Context features for the (query, site) source-selection decision.

``feature_vector`` returns a fixed-length numpy array whose ordering matches
``FEATURE_NAMES``; the same vector feeds both the greedy cosine baseline
(Phase 2) and the contextual bandit (Phase 4). The headline features are the
query-vs-site-profile cosines (``cos_bow`` / ``cos_cng``); the rest are site
priors, online per-site stats, and cheap query-shape signals. Which features
actually earn their keep is decided by the offline evaluation (Phase 5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from django.conf import settings

from mwmbl.tinysearchengine.super_search_select import vectors
from mwmbl.tinysearchengine.super_search_select.registry import SiteMeta

# Stable feature order. ``bias`` is the per-arm intercept (lets the bandit learn
# a site's base success rate even with all-zero context).
FEATURE_NAMES = [
    "bias",
    "cos_bow",
    "cos_cng",
    "popularity",
    "estimated_pages",
    "contribution_ema",
    "latency_penalty",
    "failure_rate",
    "query_len",
    "has_code_token",
]
NUM_FEATURES = len(FEATURE_NAMES)

# Tokens that look like code / identifiers — a cheap signal that programming
# sources are more likely relevant. Checked against the raw (un-lowercased) query.
_CODE_RE = re.compile(r"[_/{}();]|::|->|[a-z][A-Z]|\.\w")


@dataclass
class QueryContext:
    """Per-query, site-independent context, computed once per search."""
    bow: np.ndarray
    cng: np.ndarray
    n_tokens: int
    has_code_token: bool

    @classmethod
    def build(cls, query: str, bow: np.ndarray, cng: np.ndarray) -> "QueryContext":
        return cls(
            bow=bow,
            cng=cng,
            n_tokens=len(query.split()),
            has_code_token=bool(_CODE_RE.search(query)),
        )


@dataclass
class SiteStats:
    """Online per-site stats used as features (read from Redis; defaults for cold sites)."""
    contribution_ema: float = 0.0   # mean fraction of results surviving into top-K
    latency_ema: float = 0.0        # mean response time, seconds
    failure_rate: float = 0.0       # fraction of recent queries that errored/timed out


def feature_vector(
    qctx: QueryContext,
    meta: SiteMeta,
    profile: tuple[np.ndarray | None, np.ndarray | None],
    stats: SiteStats | None = None,
) -> np.ndarray:
    """Build the context feature vector for one (query, site) pair."""
    stats = stats or SiteStats()
    bow_profile, cng_profile = profile
    timeout = settings.SUPER_SEARCH_PER_SOURCE_TIMEOUT or 1.0
    x = np.array([
        1.0,                                          # bias
        vectors.cosine(qctx.bow, bow_profile),        # cos_bow
        vectors.cosine(qctx.cng, cng_profile),        # cos_cng
        meta.popularity,                              # popularity
        meta.estimated_pages,                         # estimated_pages
        stats.contribution_ema,                       # contribution_ema
        min(stats.latency_ema / timeout, 1.0),        # latency_penalty (0..1)
        stats.failure_rate,                           # failure_rate
        min(qctx.n_tokens, 10) / 10.0,                # query_len (normalised)
        1.0 if qctx.has_code_token else 0.0,          # has_code_token
    ], dtype=np.float64)
    return x


def cosine_relevance(qctx: QueryContext, profile: tuple[np.ndarray | None, np.ndarray | None]) -> float:
    """Greedy relevance score for the Phase-2 baseline: blended query-vs-profile cosine.

    Char n-grams are weighted lower; they mainly help on morphology / rare tokens
    where whole-word BoW misses. A site with no profile yet scores 0 (cold).
    """
    bow_profile, cng_profile = profile
    return 0.7 * vectors.cosine(qctx.bow, bow_profile) + 0.3 * vectors.cosine(qctx.cng, cng_profile)
