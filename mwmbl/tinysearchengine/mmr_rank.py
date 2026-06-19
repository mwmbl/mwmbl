"""
Maximal Marginal Relevance (MMR) diversity re-ranking.

MMRRanker is a decorator that wraps any Ranker and re-orders its relevance-sorted
search() output so near-duplicate / same-domain results are demoted rather than dropped
(the big search engines diversify the list instead of hard-capping one result per domain).
It can wrap any ranker (LTRRanker, HeuristicRanker, ...), which also makes it easy to
evaluate a ranker with and without diversity.
"""
import math
from collections import Counter
from urllib.parse import urlparse

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import Ranker
from mwmbl.tokenizer import tokenize


# MMR tuning parameters.
MMR_LAMBDA = 0.7  # weight on relevance vs. diversity (1.0 = pure relevance, 0.0 = max diversity)
DOMAIN_SIMILARITY_WEIGHT = 0.8  # within the kernel: weight on same-domain vs. text overlap
MMR_WINDOW = 50  # only diversify the top candidates; the long tail keeps relevance order


def _normalized_bow(doc: Document) -> dict[str, float]:
    """L2-normalised bag-of-words over title + extract, so cosine is a plain dot product."""
    counts = Counter(tokenize(f"{doc.title or ''} {doc.extract or ''}"))
    if not counts:
        return {}
    norm = math.sqrt(sum(c * c for c in counts.values()))
    return {token: count / norm for token, count in counts.items()}


def _text_cosine(a: dict[str, float], b: dict[str, float]) -> float:
    # a and b are already L2-normalised, so the sparse dot product is the cosine.
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b[token] for token, weight in a.items() if token in b)


def mmr_rerank(ranked_pages: list[Document]) -> list[Document]:
    """Re-order a relevance-sorted list to demote near-duplicate / same-domain results.

    Greedy Maximal Marginal Relevance with a domain-dominant kernel
    (sim = w_domain * same_domain + (1 - w_domain) * bag-of-words cosine). Relevance is
    rank-based (scale-invariant): the i-th most relevant page has relevance
    (window - i) / window, so it does not depend on the model's compressed score
    magnitudes. Each candidate is discounted by its greatest similarity to an
    already-selected page, so e.g. the second result from a domain sinks below fresher
    domains but is never dropped.

    Only the top MMR_WINDOW candidates are diversified (O(window^2)); the long tail,
    which is rarely seen, keeps plain relevance order so the cost stays bounded.
    """
    n = len(ranked_pages)
    if n <= 2:
        return ranked_pages

    window = min(n, MMR_WINDOW)
    head, tail = ranked_pages[:window], ranked_pages[window:]

    relevance = [(window - i) / window for i in range(window)]
    bows = [_normalized_bow(p) for p in head]
    netlocs = [urlparse(p.url).netloc for p in head]

    remaining = set(range(window))
    max_sim = [0.0] * window
    selected: list[int] = []
    while remaining:
        best = max(remaining, key=lambda i: MMR_LAMBDA * relevance[i] - (1 - MMR_LAMBDA) * max_sim[i])
        selected.append(best)
        remaining.discard(best)
        best_bow, best_netloc = bows[best], netlocs[best]
        for j in remaining:
            domain_sim = DOMAIN_SIMILARITY_WEIGHT if best_netloc and best_netloc == netlocs[j] else 0.0
            sim = domain_sim + (1 - DOMAIN_SIMILARITY_WEIGHT) * _text_cosine(best_bow, bows[j])
            if sim > max_sim[j]:
                max_sim[j] = sim
    return [head[i] for i in selected] + tail


class MMRRanker:
    """Decorator that applies MMR diversity re-ranking to a wrapped ranker's results.

    Demotes (never drops) same-domain / near-duplicate results. Delegates completion and
    raw retrieval unchanged.
    """

    def __init__(self, ranker: Ranker):
        self.ranker = ranker

    def search(self, s: str, additional_results: list[Document]) -> list[Document]:
        return mmr_rerank(self.ranker.search(s, additional_results))

    def complete(self, q: str):
        return self.ranker.complete(q)

    def get_raw_results(self, query: str):
        return self.ranker.get_raw_results(query)
