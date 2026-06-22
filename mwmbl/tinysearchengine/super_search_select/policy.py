"""Source-selection policy: pick which ~10 of ~100 sources to query.

Two interchangeable scorings over the same context features:

- **cosine baseline** (default): greedy query-vs-profile cosine, with a few
  reserved slots for cold sites so new sources are discovered.
- **Thompson sampling** (``SUPER_SEARCH_USE_BANDIT``): per-arm linear-Gaussian
  posterior sampling, which explores cold arms automatically via their wide
  posterior.

Either way the always-on global sources (own index, HN) are included for free,
and the feature vectors used for the decision are stashed on the
``SelectionContext`` so the bandit update at request completion is consistent
with the action taken.
"""
from __future__ import annotations

import random

import numpy as np
from django.conf import settings

from mwmbl.tinysearchengine.super_search_select import bandit, profiles
from mwmbl.tinysearchengine.super_search_select.features import (
    QueryContext,
    SiteStats,
    cosine_relevance,
    feature_vector,
)
from mwmbl.tinysearchengine.super_search_select.registry import get_meta
from mwmbl.tinysearchengine.super_search_select.rewards import SelectionContext


def select_sources(
    query: str,
    source_names: list[str],
    k: int | None = None,
    ctx: SelectionContext | None = None,
) -> list[str]:
    """Return up to ``k`` source names to query for ``query``.

    If ``ctx`` is given, the feature vector each selected source was scored on is
    recorded in ``ctx.features`` for a consistent bandit update later.
    """
    if k is None:
        k = settings.SUPER_SEARCH_SOURCES_TO_QUERY
    if len(source_names) <= k:
        if ctx is not None:
            _record_features(ctx, query, source_names)
        return list(source_names)

    always_on = [n for n in source_names if get_meta(n).always_on]
    selectable = [n for n in source_names if n not in set(always_on)]
    budget = max(k - len(always_on), 0)
    if budget == 0:
        chosen = always_on[:k]
        if ctx is not None:
            _record_features(ctx, query, chosen)
        return chosen

    bow, cng = profiles.get_query_vectors(query)
    qctx = QueryContext.build(query, bow, cng)
    profs = profiles.get_profiles(selectable)
    feats = {n: feature_vector(qctx, get_meta(n), profs[n]) for n in selectable}

    if settings.SUPER_SEARCH_USE_BANDIT:
        chosen = _select_bandit(selectable, feats, budget)
    else:
        chosen = _select_cosine(qctx, selectable, profs, budget)

    if ctx is not None:
        for name in always_on + chosen:
            if name in feats:
                ctx.features[name] = feats[name].tolist()
            elif name not in ctx.features:
                # always-on sources weren't scored; compute their features too.
                ctx.features[name] = feature_vector(
                    qctx, get_meta(name), profs.get(name, (None, None))
                ).tolist()

    return always_on + chosen


def _select_cosine(qctx, selectable, profs, budget) -> list[str]:
    warm = [n for n in selectable if profs[n][0] is not None]
    cold = [n for n in selectable if profs[n][0] is None]
    explore_n = min(getattr(settings, "SUPER_SEARCH_EXPLORE_FLOOR", 0), len(cold), budget)
    exploit_n = budget - explore_n
    warm.sort(key=lambda n: cosine_relevance(qctx, profs[n]), reverse=True)
    chosen = warm[:exploit_n]
    chosen += random.sample(cold, explore_n) if explore_n else []
    if len(chosen) < budget:
        remaining = [n for n in warm[exploit_n:] + cold if n not in set(chosen)]
        chosen += remaining[: budget - len(chosen)]
    return chosen


def _select_bandit(selectable, feats, budget) -> list[str]:
    rng = np.random.default_rng()
    states = bandit.get_states(selectable)
    scored = sorted(
        selectable,
        key=lambda n: bandit.sample_score(states[n], np.asarray(feats[n]), rng),
        reverse=True,
    )
    return scored[:budget]


def _record_features(ctx: SelectionContext, query: str, names: list[str]) -> None:
    """Compute and stash feature vectors for ``names`` (small-fanout / all-selected case)."""
    bow, cng = profiles.get_query_vectors(query)
    qctx = QueryContext.build(query, bow, cng)
    profs = profiles.get_profiles(names)
    for name in names:
        ctx.features[name] = feature_vector(
            qctx, get_meta(name), profs.get(name, (None, None)), SiteStats()
        ).tolist()
