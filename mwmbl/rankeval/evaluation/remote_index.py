"""
Pretend to be an index but retrieve results from a remote index.
"""

import time
from logging import getLogger

import requests

from mwmbl.crawler.env_vars import MWMBL_REMOTE_SERVER
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.utils import request_cache

logger = getLogger(__name__)

# This client names itself, for the same reason the crawler names itself when it fetches
# somebody else's site: whoever reads the logs should be able to tell what a request is and
# who to talk to about it. Without a User-Agent these arrive as python-requests/x.y.z, which
# is indistinguishable from any other script - and since crawl.run_indexing calls this once
# per term on every indexing pass, that is a large and permanently anonymous share of
# api.mwmbl.org's traffic. Version it separately from CRAWLER_VERSION: importing that would
# pull justext and the SSRF guard into every rankeval script for the sake of a string.
REMOTE_INDEX_VERSION = "0.1.0"
USER_AGENT = f"mwmbl-remote-index/{REMOTE_INDEX_VERSION} (+https://github.com/mwmbl/mwmbl)"


class RemoteIndex:
    def __init__(self, remote_server: str = MWMBL_REMOTE_SERVER):
        self.remote_server = remote_server
        self.url = f"{remote_server}/api/v1/search/raw?s="

    def retrieve(self, query: str, refresh: bool = False):
        url = self.url + query
        response = None
        with request_cache() as session:
            session.headers["User-Agent"] = USER_AGENT
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
