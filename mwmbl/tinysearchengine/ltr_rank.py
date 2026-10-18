"""
LTR (Learning-to-Rank) ranker that uses the Rust XGBoost pipeline for scoring.

LTRRanker accepts any model with a sklearn-compatible predict(DataFrame) interface,
including both the Python sklearn pipeline and the Rust RustXGBPipeline.
"""

import numpy as np

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, TinyIndex
from mwmbl.tinysearchengine.rank import NUM_WIKI_RESULTS, Ranker, get_wiki_results
from mwmbl.tinysearchengine.staan import STAAN_TOP_SCORE


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
        Maximum number of Wikipedia results to include. Leave it alone unless you are
        retraining: the LTR dataset is built with the same default, and a serving pool of a
        different size to the trained one is what NUM_WIKI_RESULTS exists to prevent.
    """

    def __init__(
        self,
        tiny_index: TinyIndex,
        completer: Completer,
        model,
        include_wiki: bool = True,
        num_wiki_results: int = NUM_WIKI_RESULTS,
    ):
        super().__init__(tiny_index, completer)
        self.model = model
        self.include_wiki = include_wiki
        self.num_wiki_results = num_wiki_results

    def records(self, query: str, results: list[Document]) -> list[dict]:
        """The records the model scores, one per result."""
        return [
            {
                "query": query,
                "url": page.url,
                "title": page.title if page.title is not None else "",
                "extract": page.extract if page.extract is not None else "",
                "score": page.score if page.score is not None else 0.0,
            }
            for page in results
        ]

    def order_results(self, terms: list[str], results: list[Document], is_complete: bool) -> list[Document]:
        if len(results) == 0:
            return []

        query = " ".join(terms)
        predictions = self.model.predict(self.records(query, results))
        mask = predictions > 0.0
        filtered_predictions = predictions[mask]
        filtered_pages = np.array(results)[mask]
        if len(filtered_pages) == 0:
            return []

        # Sort by model relevance (descending).
        indices = np.argsort(filtered_predictions)[::-1]
        return filtered_pages[indices].tolist()

    def external_search(self, query: str) -> list[Document]:
        if self.include_wiki:
            return get_wiki_results(query, self.num_wiki_results)
        return []


class CombinedLTRRanker(LTRRanker):
    """Combined Search's ranker: tells the model what Staan made of each candidate.

    Each record carries Staan's rank for its URL, for the model's provider features, and
    whether it is Staan's own result, which exempts it from the majority-terms filter. Both
    are applied in mwmbl_rank; see DocumentRecord there.

    Staan counts as asked when any of its results reached the pool. A Staan that is down
    returns nothing, which is then indistinguishable from Staan having no results: both
    score as "not asked", the same as the index-only queries in the training data.
    """

    def __init__(self, tiny_index: TinyIndex, completer: Completer, model):
        # The endpoint fetches Staan itself and passes it in as additional results.
        super().__init__(tiny_index, completer, model, include_wiki=False)

    def records(self, query: str, results: list[Document]) -> list[dict]:
        # Each Staan result carries its rank in its score (see staan_score), which survives
        # the blacklist filter that would throw off counting positions.
        staan_ranks = {
            page.url: round(STAAN_TOP_SCORE - page.score) for page in results if page.source == DocumentSource.STAAN
        }
        staan_asked = len(staan_ranks) > 0
        records = super().records(query, results)
        for record, page in zip(records, results):
            record["staan_asked"] = staan_asked
            record["staan_rank"] = staan_ranks.get(page.url)
            record["from_staan"] = page.source == DocumentSource.STAAN
        return records


def score_documents(model, query: str, documents: list[Document]) -> list[float]:
    """Run the LTR model over the given documents and return raw per-doc scores.

    Sync — call from a thread when used inside an async context.
    """
    if not documents:
        return []
    data = [
        {
            "query": query,
            "url": page.url,
            "title": page.title if page.title is not None else "",
            "extract": page.extract if page.extract is not None else "",
            "score": page.score if page.score is not None else 0.0,
        }
        for page in documents
    ]
    predictions = model.predict(data)
    return [float(p) for p in predictions]
