import numpy as np
from pandas import DataFrame
from sklearn.base import BaseEstimator

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.ltr import FeatureExtractor
from mwmbl.tinysearchengine.rank import Ranker
from mwmbl.tokenizer import tokenize


class LTRRanker:
    def __init__(self, base_ranker: Ranker, model: BaseEstimator, top_n: int = 20):
        self.base_ranker = base_ranker
        self.model = model
        self.top_n = top_n

    def search(self, query: str, additional_results: list[Document]) -> list[Document]:
        pages = self.base_ranker.search(query, additional_results)
        if len(pages) == 0:
            return []

        top_pages = pages[:self.top_n]

        data = {
            'query': [query] * len(top_pages),
            'url': [page.url for page in top_pages],
            'title': [page.title for page in top_pages],
            'extract': [page.extract for page in top_pages],
            'score': [page.score for page in top_pages],
        }

        dataframe = DataFrame(data)
        # feature_extractor = FeatureExtractor()
        # features = feature_extractor.transform(dataframe)

        print("Ordering results", dataframe)
        predictions = self.model.predict(dataframe)
        indexes = np.argsort(predictions)[::-1]
        return [top_pages[i] for i in indexes]
