"""
Pretend to be an index but retrieve results from a remote index.
"""
import requests
from joblib import Memory

from mwmbl.tinysearchengine.indexer import Document

memory = Memory(location='devdata/cache')


@memory.cache
def retrieve_url(url: str):
    response = requests.get(url)
    print(f"Retrieved {url}", response.content)
    return response.json()


class RemoteIndex:
    url = "https://mwmbl.org/api/v1/search/raw?s="

    def retrieve(self, query: str):
        url = self.url + query
        results = retrieve_url(url)
        return [Document(**result) for result in results["results"]]
