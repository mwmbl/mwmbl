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


def _bag_of_words(doc: Document) -> Counter:
    return Counter(tokenize(f"{doc.title or ''} {doc.extract or ''}"))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(count * b[token] for token, count in a.items() if token in b)
    if dot == 0:
        return 0.0
    norm_a = math.sqrt(sum(c * c for c in a.values()))
    norm_b = math.sqrt(sum(c * c for c in b.values()))
    return dot / (norm_a * norm_b)


def _similarity(bow_a: Counter, netloc_a: str, bow_b: Counter, netloc_b: str) -> float:
    """Domain-dominant similarity: same-domain pairs are penalised hardest, with a
    bag-of-words cosine as a secondary signal to catch cross-domain near-duplicates."""
    domain_sim = 1.0 if netloc_a and netloc_a == netloc_b else 0.0
    text_sim = _cosine(bow_a, bow_b)
    return DOMAIN_SIMILARITY_WEIGHT * domain_sim + (1 - DOMAIN_SIMILARITY_WEIGHT) * text_sim


def mmr_rerank(ranked_pages: list[Document]) -> list[Document]:
    """Re-order a relevance-sorted list to demote near-duplicate / same-domain results.

    Greedy Maximal Marginal Relevance with the domain-dominant kernel above. Relevance
    is rank-based (scale-invariant): the i-th most relevant page has relevance
    (n - i) / n, so it does not depend on the model's compressed score magnitudes.
    Each candidate is discounted by its greatest similarity to an already-selected page,
    so e.g. the second result from a domain sinks below fresher domains but is never
    dropped. Complexity is O(n^2) over the (small) per-query candidate set.
    """
    n = len(ranked_pages)
    if n <= 2:
        return ranked_pages

    relevance = [(n - i) / n for i in range(n)]
    bows = [_bag_of_words(p) for p in ranked_pages]
    netlocs = [urlparse(p.url).netloc for p in ranked_pages]

    remaining = set(range(n))
    max_sim = [0.0] * n
    selected: list[int] = []
    while remaining:
        best = max(remaining, key=lambda i: MMR_LAMBDA * relevance[i] - (1 - MMR_LAMBDA) * max_sim[i])
        selected.append(best)
        remaining.discard(best)
        for j in remaining:
            sim = _similarity(bows[best], netlocs[best], bows[j], netlocs[j])
            if sim > max_sim[j]:
                max_sim[j] = sim
    return [ranked_pages[i] for i in selected]


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
