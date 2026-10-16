"""
Pretend to be an index but retrieve results from a remote index.
"""

import time
from datetime import timedelta
from logging import getLogger
from typing import Optional

import requests

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.utils import request_cache

logger = getLogger(__name__)


class RemoteIndex:
    def __init__(self, remote_server: str = "https://api.mwmbl.org", expire_after: Optional[timedelta] = None):
        """expire_after is how long a cached copy of the index's results may be reused.

        None never expires it, which is what the evaluation scripts want: they replay one
        set of queries over and over against an index that moves slowly enough not to
        matter. A caller that runs for weeks against a moving index wants a real expiry -
        see REMOTE_INDEX_CACHE_EXPIRY in crawl.py.
        """
        self.remote_server = remote_server
        self.expire_after = expire_after
        self.url = f"{remote_server}/api/v1/search/raw?s="

    def retrieve(self, query: str, refresh: bool = False):
        url = self.url + query
        response = None
        with request_cache(self.expire_after) as session:
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
