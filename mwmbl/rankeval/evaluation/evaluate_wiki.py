"""
Evaluate ranking where we only use Wikipedia as the index.
"""
import urllib

import requests
from joblib import Memory

from mwmbl.rankeval.evaluation.evaluate import evaluate
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import WIKI_SEARCH_API_URL, get_wiki_url

memory = Memory(location="devdata/cache")


@memory.cache
def fetch_results(query: str) -> list[str]:
    safe_query = urllib.parse.quote(query, safe="")
    url = WIKI_SEARCH_API_URL.format(query=safe_query)
    print("Getting url", url)
    results = requests.get(url).json()
    print("Results", results)
    return [get_wiki_url(result['title']) for result in results['query']['search']]


class WikiModel:
    def predict(self, query):
        return fetch_results(query)


def run():
    model = WikiModel()
    evaluate(model, fraction=0.01)


if __name__ == "__main__":
    run()
