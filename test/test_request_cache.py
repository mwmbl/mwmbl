"""Where the crawler's cache of index results lives, and what stops it growing.

A volunteer crawler was found with a full disk. 8.95GB of it was this cache: run_indexing
asks the main index what it holds for each of a round's top 100 terms, and every distinct
term left a response on disk that nothing ever expired or removed. It was not even on the
volume - settings_crawler inherited a REQUEST_CACHE_PATH of ./devdata/request_cache from
settings_dev, which resolves under the container's working directory - so it grew invisibly
in the writable layer and a restart silently reset it.
"""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import requests
import urllib3
from django.test import override_settings
from requests_cache import CachedSession

from mwmbl import settings_crawler
from mwmbl.crawl import REMOTE_INDEX_CACHE_EXPIRY
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.utils import prune_request_cache


def expiry_used_by(remote_index: RemoteIndex):
    """The expire_after that a retrieve asks request_cache for."""
    session = MagicMock()
    session.get.return_value.json.return_value = {"results": []}
    with patch("mwmbl.rankeval.evaluation.remote_index.request_cache") as request_cache:
        request_cache.return_value.__enter__.return_value = session
        remote_index.retrieve("example")
    return request_cache.call_args.args[0]


def test_crawler_request_cache_is_under_the_data_path():
    assert settings_crawler.REQUEST_CACHE_PATH.startswith(settings_crawler.DATA_PATH)


def test_the_crawler_expires_what_the_index_told_it():
    assert expiry_used_by(RemoteIndex(expire_after=REMOTE_INDEX_CACHE_EXPIRY)) == REMOTE_INDEX_CACHE_EXPIRY


def test_an_unasked_expiry_still_never_expires():
    """The evaluation scripts replay one set of queries and want the cache to keep them."""
    assert expiry_used_by(RemoteIndex()) is None


def response_for(url: str) -> requests.Response:
    """A response complete enough for the cache to store, built without going near a network."""
    request = requests.Request("GET", url).prepare()
    raw = urllib3.HTTPResponse(body=BytesIO(b"{}"), status=200, preload_content=False, request_url=url)
    return requests.adapters.HTTPAdapter().build_response(request, raw)


def test_pruning_deletes_what_has_expired_and_keeps_the_rest(tmp_path):
    cache_path = str(tmp_path / "request_cache")
    now = datetime.now(timezone.utc)
    with override_settings(REQUEST_CACHE_PATH=cache_path):
        with CachedSession(backend="filesystem", cache_name=cache_path) as session:
            session.cache.save_response(
                response_for("https://example.com/stale"), cache_key="stale", expires=now - timedelta(hours=1)
            )
            session.cache.save_response(
                response_for("https://example.com/fresh"), cache_key="fresh", expires=now + timedelta(hours=1)
            )

            prune_request_cache()

            assert not session.cache.contains("stale")
            assert session.cache.contains("fresh")
