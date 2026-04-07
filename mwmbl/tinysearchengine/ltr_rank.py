"""
LTR (Learning-to-Rank) ranker that uses the Rust XGBoost pipeline for scoring.

LTRRanker accepts any model with a sklearn-compatible predict(DataFrame) interface,
including both the Python sklearn pipeline and the Rust RustXGBPipeline.
"""
import numpy as np
from pandas import DataFrame
from sklearn.base import BaseEstimator

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.rank import Ranker, get_wiki_results


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
        top_n: int,
        include_wiki: bool = True,
        num_wiki_results: int = 5,
    ):
        super().__init__(tiny_index, completer)
        self.model = model
        self.top_n = top_n
        self.include_wiki = include_wiki
        self.num_wiki_results = num_wiki_results

    def order_results(self, terms: list[str], results: list[Document], is_complete: bool) -> list[Document]:
        if len(results) == 0:
            return []

        query = ' '.join(terms)
        top_pages = results[:self.top_n]

        data = [{
            'query': query,
            'url': page.url,
            'title': page.title if page.title is not None else "",
            'extract': page.extract if page.extract is not None else "",
            'score': page.score if page.score is not None else 0.0,
        }  for page in top_pages]

        predictions = self.model.predict(data)
        mask = predictions > 0.0
        filtered_predictions = predictions[mask]
        filtered_pages = np.array(top_pages)[mask]

        # Vectorized sorting
        indices = np.argsort(filtered_predictions)[::-1]
        return filtered_pages[indices].tolist()

    def external_search(self, query: str) -> list[Document]:
        if self.include_wiki:
            return get_wiki_results(query, self.num_wiki_results)
        return []
