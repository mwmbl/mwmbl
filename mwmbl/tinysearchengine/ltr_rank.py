import numpy as np
from pandas import DataFrame
from sklearn.base import BaseEstimator

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.ltr import FeatureExtractor
from mwmbl.tinysearchengine.rank import Ranker, HeuristicRanker, get_wiki_results
from mwmbl.tokenizer import tokenize


class LTRRanker(HeuristicRanker):
    def __init__(self, tiny_index: TinyIndex, completer: Completer, model: BaseEstimator,
                 top_n: int, include_wiki: bool = True, num_wiki_results: int = 5):
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
        data = {
            'query': [query] * len(top_pages),
            'url': [page.url for page in top_pages],
            'title': [page.title for page in top_pages],
            'extract': [page.extract for page in top_pages],
            'score': [page.score for page in top_pages],
        }

        dataframe = DataFrame(data)

        print("Ordering results", dataframe)
        predictions = self.model.predict(dataframe)
        indexes = np.argsort(predictions)[::-1]
        return [top_pages[i] for i in indexes]

    def external_search(self, query: str) -> list[Document]:
        if self.include_wiki:
            return get_wiki_results(query, self.num_wiki_results)
        return []
