"""
Pretend to be an index but retrieve results from a remote index.
"""
import time
from logging import getLogger

import requests

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.utils import request_cache


logger = getLogger(__name__)


class RemoteIndex:
    url = "https://beta.mwmbl.org/api/v1/search/raw?s="

    def retrieve(self, query: str, refresh: bool = False):
        url = self.url + query
        response = None
        with request_cache() as session:
            for i in range(3):
                try:
                    response = session.get(url, timeout=15, force_refresh=refresh)
                    break
                except requests.exceptions.Timeout:
                    logger.info(f"Timeout fetching {url}, sleeping")
                    time.sleep(1)
        if response is None:
            raise ValueError(f"Failed to fetch {url}")
        results = response.json()
        return [Document(**result) for result in results["results"]]
