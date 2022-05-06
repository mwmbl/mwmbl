import numpy as np
from pandas import DataFrame
from sklearn.base import BaseEstimator

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.rank import Ranker, order_results


class LTRRanker(Ranker):
    def __init__(self, model: BaseEstimator, tiny_index: TinyIndex, completer: Completer):
        super().__init__(tiny_index, completer)
        self.model = model
        self.top_n = 20

    def order_results(self, terms, pages: list[Document], is_complete):
        if len(pages) == 0:
            return []

        top_pages = order_results(terms, pages, is_complete)[:self.top_n]

        query = ' '.join(terms)
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
