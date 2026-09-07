"""Implicit reward and impression logging for source selection.

The preferred reward for a queried source is the mean relevance-judge score of
the documents it contributed to the final pool (``compute_judge_rewards``; see
super_search_select/judge.py) — an independent signal validated against LLM
labels and human curations. When the judge is unavailable the fallback is the
original survival reward: the fraction of a source's results in the final
LTR-ranked top-K (``compute_rewards``). ``SelectionContext`` carries the
per-request state (which sources were selected, which URL came from which
source, and the judge scores from the final re-rank) from the pipeline to the
completion hook, where reward computation and ``log_impression`` run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class SelectionContext:
    """Per-request selection state, populated as the pipeline runs."""

    candidates: list[str] = field(default_factory=list)  # full action space
    selected: list[str] = field(default_factory=list)  # sources actually queried
    source_by_url: dict[str, str] = field(default_factory=dict)  # url -> originating source
    features: dict[str, list[float]] = field(default_factory=dict)  # source -> feature vector
    judge_scores: dict[str, float] = field(default_factory=dict)  # url -> judge score (final pool)
    per_source_limit: int = 0

    def record_results(self, source: str, urls: list[str]) -> None:
        """Note the URLs a source returned (first source to produce a URL wins)."""
        for url in urls:
            self.source_by_url.setdefault(url, source)


def compute_rewards(ctx: SelectionContext, final_top_k_urls: list[str]) -> dict[str, float]:
    """Reward per selected source = fraction of its results in the final top-K.

    Normalised by ``per_source_limit`` so a source returning many top-K results
    scores near 1.0 and one returning none scores 0.0. Every selected source gets
    an entry (0.0 if it contributed nothing) — the bandit needs the zeros too.
    """
    limit = max(ctx.per_source_limit, 1)
    rewards = {name: 0.0 for name in ctx.selected}
    for url in final_top_k_urls:
        source = ctx.source_by_url.get(url)
        if source in rewards:
            rewards[source] += 1.0 / limit
    return {name: min(r, 1.0) for name, r in rewards.items()}


def compute_judge_rewards(ctx: SelectionContext) -> dict[str, float] | None:
    """Reward per selected source = mean judge score of its final-pool documents.

    Uses the per-URL relevance-judge scores recorded during the final re-rank
    (``ctx.judge_scores``). The mean over a source's contributed documents is
    the aggregate the judge was validated on (source-level agreement in the
    bake-off). Every selected source gets an entry — 0.0 when nothing it
    returned reached the final pool, since the bandit needs the zeros too.
    Returns None when no judge scores exist (judge unavailable or no results);
    callers fall back to ``compute_rewards``.
    """
    if not ctx.judge_scores:
        return None
    totals = {name: [0.0, 0] for name in ctx.selected}
    for url, score in ctx.judge_scores.items():
        source = ctx.source_by_url.get(url)
        if source in totals:
            totals[source][0] += score
            totals[source][1] += 1
    return {name: total / count if count else 0.0 for name, (total, count) in totals.items()}


def log_impression(ctx: SelectionContext, rewards: dict[str, float]) -> None:
    """Persist a SuperSearchImpression row (no-op without a database)."""
    if not getattr(settings, "HAS_DATABASE", False):
        return
    try:
        from mwmbl.models import SuperSearchImpression

        SuperSearchImpression.objects.create(
            candidates=ctx.candidates,
            selected=ctx.selected,
            features=ctx.features,
            rewards=rewards,
        )
    except Exception:
        logger.exception("failed to log super-search impression")


def record_source_provenance(ctx: SelectionContext) -> None:
    """Persist a SourceProvenance row per (url, source) Super Search returned.

    Records the durable url -> source mapping (depth 0) so source usefulness can
    be judged offline, including for pages crawled later from these URLs. No-op
    without a database; conflicts on the unique url are ignored (first source to
    produce a URL wins, matching SelectionContext.record_results).
    """
    if not getattr(settings, "HAS_DATABASE", False):
        return
    if not ctx.source_by_url:
        return
    try:
        from mwmbl.models import SourceProvenance

        rows = [SourceProvenance(url=url, source=source, depth=0) for url, source in ctx.source_by_url.items()]
        SourceProvenance.objects.bulk_create(rows, ignore_conflicts=True)
    except Exception:
        logger.exception("failed to record super-search source provenance")
