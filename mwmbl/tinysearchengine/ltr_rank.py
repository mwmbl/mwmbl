import numpy as np
from pandas import DataFrame
from sklearn.base import BaseEstimator

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.rank import Ranker


class LTRRanker(Ranker):
    def __init__(self, model: BaseEstimator, tiny_index: TinyIndex, completer: Completer):
        super().__init__(tiny_index, completer)
        self.model = model

    def order_results(self, terms, pages: list[Document], is_complete):
        if len(pages) == 0:
            return []

        query = ' '.join(terms)
        data = {
            'query': [query] * len(pages),
            'url': [page.url for page in pages],
            'title': [page.title for page in pages],
            'extract': [page.extract for page in pages],
            'score': [page.score for page in pages],
        }

        dataframe = DataFrame(data)
        print("Ordering results", dataframe)
        predictions = self.model.predict(dataframe)
        indexes = np.argsort(predictions)[::-1]
        return [pages[i] for i in indexes]
