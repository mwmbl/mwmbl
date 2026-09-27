"""The crawl model as a write-time page ranker, for build_index.py's crawl arms.

It lives here rather than in mwmbl/indexer while it is an experiment: the domain features are
computed in Python, and would move into mwmbl_rank if the arm wins.
"""

import pandas as pd
import xgboost as xgb
from crawl_model import MODEL_DIR, all_features, feature_names

from mwmbl.tinysearchengine.indexer import Document


class CrawlPageRanker:
    """Orders a term's documents by the crawl model, dropping nothing, like LTRPageRanker."""

    def __init__(self, name: str):
        self.use_domain = name == "crawl-domain"
        self.booster = xgb.Booster()
        self.booster.load_model(MODEL_DIR / f"{name}.json")

    def order_results(self, terms: list[str], documents: list[Document], is_complete: bool) -> list[Document]:
        rows = pd.DataFrame(
            {
                "term": " ".join(terms),
                "url": [document.url for document in documents],
                "title": [document.title or "" for document in documents],
                "extract": [document.extract or "" for document in documents],
                "qnorm": None,
            }
        )
        features = all_features(rows, self.use_domain)
        scores = self.booster.predict(xgb.DMatrix(features, feature_names=feature_names(self.use_domain)))
        order = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
        return [documents[i] for i in order]
