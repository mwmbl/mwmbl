"""
LTR (Learning-to-Rank) ranker that uses the Rust XGBoost pipeline for scoring.

LTRRanker accepts any model with a sklearn-compatible predict(DataFrame) interface,
including both the Python sklearn pipeline and the Rust RustXGBPipeline.
"""
import math
from collections import Counter
from urllib.parse import urlparse

import numpy as np
from pandas import DataFrame
from sklearn.base import BaseEstimator

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.rank import Ranker, get_wiki_results
from mwmbl.tokenizer import tokenize


# Maximal Marginal Relevance (MMR) diversity parameters, applied in order_results.
# MMR re-orders the relevance-sorted candidates so near-duplicate / same-domain
# results are demoted rather than dropped (the big search engines diversify the
# list instead of hard-capping one result per domain).
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


class LTRRanker(Ranker):
    """
    Learning-to-rank ranker.

    Accepts any model with a predict(DataFrame) -> array interface.
    The DataFrame passed to predict has columns: query, url, title, extract, score.

    Compatible with:
    - sklearn Pipeline (e.g. make_pipeline(FeatureExtractor(), ThresholdPredictor(...)))
    - RustXGBPipeline (Rust-backed, much faster feature extraction)

    Parameters
    ----------
    tiny_index : TinyIndex
        The search index.
    completer : Completer
        Query completer.
    model : BaseEstimator or RustXGBPipeline
        Trained ranking model with a predict(DataFrame) method.
    top_n : int
        Maximum number of candidates to score (for efficiency).
    include_wiki : bool
        Whether to include Wikipedia results via external search.
    num_wiki_results : int
        Maximum number of Wikipedia results to include.
    """

    def __init__(
        self,
        tiny_index: TinyIndex,
        completer: Completer,
        model,
        include_wiki: bool = True,
        num_wiki_results: int = 5,
    ):
        super().__init__(tiny_index, completer)
        self.model = model
        self.include_wiki = include_wiki
        self.num_wiki_results = num_wiki_results

    def order_results(self, terms: list[str], results: list[Document], is_complete: bool) -> list[Document]:
        if len(results) == 0:
            return []

        query = ' '.join(terms)

        data = [{
            'query': query,
            'url': page.url,
            'title': page.title if page.title is not None else "",
            'extract': page.extract if page.extract is not None else "",
            'score': page.score if page.score is not None else 0.0,
        }  for page in results]

        predictions = self.model.predict(data)
        mask = predictions > 0.0
        filtered_predictions = predictions[mask]
        filtered_pages = np.array(results)[mask]
        if len(filtered_pages) == 0:
            return []

        # Sort by model relevance, then re-order with MMR to diversify the list
        # (demotes same-domain / near-duplicate results instead of dropping them).
        indices = np.argsort(filtered_predictions)[::-1]
        ranked_pages = filtered_pages[indices].tolist()
        return mmr_rerank(ranked_pages)

    def external_search(self, query: str) -> list[Document]:
        if self.include_wiki:
            return get_wiki_results(query, self.num_wiki_results)
        return []


def score_documents(model, query: str, documents: list[Document]) -> list[float]:
    """Run the LTR model over the given documents and return raw per-doc scores.

    Sync — call from a thread when used inside an async context.
    """
    if not documents:
        return []
    data = [{
        'query': query,
        'url': page.url,
        'title': page.title if page.title is not None else "",
        'extract': page.extract if page.extract is not None else "",
        'score': page.score if page.score is not None else 0.0,
    } for page in documents]
    predictions = model.predict(data)
    return [float(p) for p in predictions]
