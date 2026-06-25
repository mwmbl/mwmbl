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

# Query-intent taxonomy. Each query is tagged with zero or more coarse intents by
# cheap word-boundary lexicon rules; appended to the feature vector as a one-hot
# block. Because the tags are constant across all sources of a given query, only
# the *per-arm* bandit can exploit them — each arm's ``theta`` learns that source's
# affinity for each intent from realized reward (the global XGBoost in ``select``,
# which ranks sources within a query, is structurally blind to query-only features,
# so judge intents via ``simulate``). Generalises ``has_code_token`` from one bit
# to a vector; the dense (intent, source) co-occurrence is what makes this learnable
# where raw per-term one-hot is ~93% singletons on the eval matrix.
INTENT_NAMES = [
    "code",       # programming / software / dev tooling
    "academic",   # papers / research / formal study
    "gaming",     # video games, mods, speedruns
    "music",      # songs, lyrics, albums, instruments
    "news",       # current events / politics / recency
    "howto",      # questions / guides / instructional
    "media",      # books / film / art / reviews
    "reference",  # definitions / encyclopedic / "what is"
]

_INTENT_PATTERNS = {
    "code": re.compile(
        r"\b(python|javascript|typescript|rust|golang|c\+\+|api|sdk|cli|npm|pip|"
        r"pypi|github|gitlab|git|docker|kubernetes|regex|compiler|runtime|library|"
        r"framework|package|module|function|async|bug|error|exception|traceback|"
        r"syntax|code|coding|programming|developer|debug|install)\b", re.I),
    "academic": re.compile(
        r"\b(paper|papers|study|studies|research|thesis|dissertation|theorem|proof|"
        r"lemma|journal|arxiv|preprint|citation|hypothesis|equation|dataset|"
        r"benchmark|algorithm|peer.?review)\b", re.I),
    "gaming": re.compile(
        r"\b(game|games|gaming|gamer|mod|mods|speedrun|playthrough|walkthrough|rpg|"
        r"fps|mmo|roguelike|pokemon|pokémon|minecraft|factorio|steam|itch|"
        r"console|xbox|playstation|nintendo)\b", re.I),
    "music": re.compile(
        r"\b(song|songs|lyric|lyrics|album|albums|band|bands|music|musician|guitar|"
        r"piano|synth|chord|chords|melody|track|tracks|remix|vinyl|discography)\b", re.I),
    "news": re.compile(
        r"\b(news|election|elections|president|senate|congress|war|breaking|latest|"
        r"today|recent|court|verdict|policy|sanctions|protest|economy|inflation|"
        r"20\d\d)\b", re.I),
    "howto": re.compile(
        r"\b(how|what|why|who|when|where|guide|guides|tutorial|tutorials|tips|howto|"
        r"learn|fix|setup|configure|versus|vs|difference)\b", re.I),
    "media": re.compile(
        r"\b(book|books|novel|novels|author|authors|film|films|movie|movies|cinema|"
        r"series|art|artist|painting|paintings|museum|gallery|poem|poetry|story|"
        r"stories|review|reviews)\b", re.I),
    "reference": re.compile(
        r"\b(define|definition|meaning|means|history|origin|encyclopedia|wiki|"
        r"wikipedia|biography|facts|overview|explained|explain)\b", re.I),
}


def classify_intent(query: str) -> list[float]:
    """One-hot the query over ``INTENT_NAMES`` via lexicon rules (multi-label)."""
    return [1.0 if _INTENT_PATTERNS[name].search(query or "") else 0.0
            for name in INTENT_NAMES]


# Stable feature order. ``bias`` is the per-arm intercept (lets the bandit learn
# a site's base success rate even with all-zero context). The ``intent_*`` block is
# appended last so existing feature indices are unchanged.
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
] + [f"intent_{name}" for name in INTENT_NAMES]
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
    intent: list[float]  # one-hot over INTENT_NAMES

    @classmethod
    def build(cls, query: str, bow: np.ndarray, cng: np.ndarray) -> "QueryContext":
        return cls(
            bow=bow,
            cng=cng,
            n_tokens=len(query.split()),
            has_code_token=bool(_CODE_RE.search(query)),
            intent=classify_intent(query),
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
        *qctx.intent,                                 # intent_* one-hot block
    ], dtype=np.float64)
    return x


def cosine_relevance(qctx: QueryContext, profile: tuple[np.ndarray | None, np.ndarray | None]) -> float:
    """Greedy relevance score for the Phase-2 baseline: blended query-vs-profile cosine.

    Char n-grams are weighted lower; they mainly help on morphology / rare tokens
    where whole-word BoW misses. A site with no profile yet scores 0 (cold).
    """
    bow_profile, cng_profile = profile
    return 0.7 * vectors.cosine(qctx.bow, bow_profile) + 0.3 * vectors.cosine(qctx.cng, cng_profile)
