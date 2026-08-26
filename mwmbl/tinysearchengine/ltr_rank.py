"""
LTR (Learning-to-Rank) ranker that uses the Rust XGBoost pipeline for scoring.

LTRRanker accepts any model with a sklearn-compatible predict(DataFrame) interface,
including both the Python sklearn pipeline and the Rust RustXGBPipeline.
"""
import numpy as np
from django.conf import settings
from pandas import DataFrame
from sklearn.base import BaseEstimator

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.rank import Ranker, get_wiki_results
from mwmbl.tinysearchengine.wiki_index_cache import have_enough_wiki_results, store_wiki_results
from mwmbl.tokenizer import tokenize


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
    wiki_fetcher : callable(query, num_results) -> list[Document]
        How Wikipedia is queried. Injectable so an evaluation can count and cache the
        calls a policy makes without actually making them repeatedly.
    wiki_store : callable(query, documents, index_path) -> int
        How fetched results are written back into the index.
    wiki_index_path : str or None
        Index to write results into. None means the configured one.
    """

    def __init__(
        self,
        tiny_index: TinyIndex,
        completer: Completer,
        model,
        include_wiki: bool = True,
        num_wiki_results: int = 5,
        wiki_fetcher=None,
        wiki_store=None,
        wiki_index_path=None,
    ):
        super().__init__(tiny_index, completer)
        self.model = model
        self.include_wiki = include_wiki
        self.num_wiki_results = num_wiki_results
        self.wiki_fetcher = wiki_fetcher if wiki_fetcher is not None else get_wiki_results
        self.wiki_store = wiki_store if wiki_store is not None else store_wiki_results
        self.wiki_index_path = wiki_index_path

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

        # Sort by model relevance (descending).
        indices = np.argsort(filtered_predictions)[::-1]
        return filtered_pages[indices].tolist()

    def external_search(self, query: str, index_results: list[Document]) -> list[Document]:
        """Wikipedia results, fetched live only when the index has not already got them.

        With WIKI_INDEX_CACHE_ENABLED off this is the historical behaviour: one API call
        per search. With it on, a fetch also writes its results into the index under the
        query terms they are allowed to take, so a later matching query is answered from
        the index and makes no call at all - see mwmbl.tinysearchengine.wiki_index_cache.
        """
        if not self.include_wiki:
            return []

        if not settings.WIKI_INDEX_CACHE_ENABLED:
            return self.wiki_fetcher(query, self.num_wiki_results)

        def rank(documents: list[Document]) -> list[Document]:
            return self.order_results(tokenize(query), documents, query.endswith(' '))

        def score(documents: list[Document]) -> list[float]:
            return score_documents(self.model, query, documents)

        if have_enough_wiki_results(query, index_results, rank, score):
            return []

        results = self.wiki_fetcher(query, self.num_wiki_results)
        self.wiki_store(query, results, self.wiki_index_path)
        return results


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
