"""The client the crawler's index sync and every rankeval script use to read the main index.

Two things are pinned here, both of which were true of api.mwmbl.org's traffic before they
were fixed. It sends a User-Agent that names it, so that its share of the API's traffic is
attributable rather than arriving as an anonymous python-requests - and the crawler's index
sync names itself more precisely, with its version and its user's public name. And the server
it talks to comes from the environment, so that an evaluation sweep is not aimed at production
by the mere fact of calling RemoteIndex() with no argument.
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest
import requests

import mwmbl.crawl as crawl
from mwmbl.crawler.retrieve import CRAWLER_VERSION
from mwmbl.rankeval.evaluation.remote_index import USER_AGENT, RemoteIndex


def _session_returning(payload):
    """A request_cache() stand-in: a context manager yielding a session that returns payload."""
    session = MagicMock()
    session.headers = {}
    session.get.return_value = MagicMock(json=MagicMock(return_value=payload))
    cache = MagicMock()
    cache.__enter__ = MagicMock(return_value=session)
    cache.__exit__ = MagicMock(return_value=False)
    return cache, session


def test_retrieve_identifies_itself():
    cache, session = _session_returning({"results": []})
    with patch("mwmbl.rankeval.evaluation.remote_index.request_cache", return_value=cache):
        RemoteIndex().retrieve("python tutorial")

    assert session.headers["User-Agent"] == USER_AGENT


def test_user_agent_names_the_project_and_a_way_to_find_it():
    assert USER_AGENT.startswith("mwmbl-remote-index/")
    assert "github.com/mwmbl/mwmbl" in USER_AGENT


def test_retrieve_reads_the_raw_endpoint_on_the_configured_server():
    cache, session = _session_returning({"results": []})
    with patch("mwmbl.rankeval.evaluation.remote_index.request_cache", return_value=cache):
        RemoteIndex("https://beta.mwmbl.org").retrieve("cat")

    url = session.get.call_args[0][0]
    assert url == "https://beta.mwmbl.org/api/v1/search/raw?s=cat"


def test_default_server_comes_from_the_environment():
    with patch.dict("os.environ", {"MWMBL_REMOTE_SERVER": "https://beta.mwmbl.org"}):
        env_vars = importlib.reload(importlib.import_module("mwmbl.crawler.env_vars"))
        assert env_vars.MWMBL_REMOTE_SERVER == "https://beta.mwmbl.org"

    # Restore the module for anything importing it later in the session.
    importlib.reload(importlib.import_module("mwmbl.crawler.env_vars"))


def test_default_server_is_production_when_unset():
    import mwmbl.crawler.env_vars as env_vars

    assert env_vars.MWMBL_REMOTE_SERVER == "https://api.mwmbl.org"
    assert RemoteIndex().remote_server == "https://api.mwmbl.org"


def test_retrieve_sends_the_user_agent_it_was_given():
    cache, session = _session_returning({"results": []})
    with patch("mwmbl.rankeval.evaluation.remote_index.request_cache", return_value=cache):
        RemoteIndex(user_agent="mwmbl-crawler/9.9.9 (+x; user swift_falcon_379)").retrieve("cat")

    assert session.headers["User-Agent"] == "mwmbl-crawler/9.9.9 (+x; user swift_falcon_379)"


# ---------------------------------------------------------------------------
# The crawler's index sync
# ---------------------------------------------------------------------------


@pytest.fixture
def forget_username():
    crawl._username = None
    yield
    crawl._username = None


def test_api_user_agent_carries_the_crawler_version_and_the_username():
    user_agent = crawl.api_user_agent("swift_falcon_379")

    assert user_agent == (f"mwmbl-crawler/{CRAWLER_VERSION} (+https://github.com/mwmbl/mwmbl; user swift_falcon_379)")


def test_api_user_agent_still_carries_the_version_without_a_username():
    assert crawl.api_user_agent(None) == f"mwmbl-crawler/{CRAWLER_VERSION} (+https://github.com/mwmbl/mwmbl)"


def test_api_user_agent_is_a_valid_header_for_a_non_ascii_username():
    user_agent = crawl.api_user_agent("zoë")

    assert "user zo%C3%AB" in user_agent
    user_agent.encode("latin-1")


def test_the_username_comes_from_the_api_key_and_is_remembered(forget_username):
    response = MagicMock(json=MagicMock(return_value={"username": "swift_falcon_379"}))
    with (
        patch("mwmbl.crawl.MWMBL_API_KEY", "a-key"),
        patch("mwmbl.crawl.requests.get", return_value=response) as get,
    ):
        assert crawl._fetch_username() == "swift_falcon_379"
        assert crawl._fetch_username() == "swift_falcon_379"

    get.assert_called_once()
    assert get.call_args.args[0] == crawl.USER_URL
    assert get.call_args.kwargs["headers"]["X-API-Key"] == "a-key"


def test_a_failed_username_lookup_is_tried_again_next_time(forget_username):
    response = MagicMock(json=MagicMock(return_value={"username": "swift_falcon_379"}))
    with (
        patch("mwmbl.crawl.MWMBL_API_KEY", "a-key"),
        patch("mwmbl.crawl.requests.get", side_effect=[requests.ConnectionError(), response]),
    ):
        assert crawl._fetch_username() is None
        assert crawl._fetch_username() == "swift_falcon_379"


def test_no_username_lookup_without_an_api_key(forget_username):
    with (
        patch("mwmbl.crawl.MWMBL_API_KEY", ""),
        patch("mwmbl.crawl.requests.get") as get,
    ):
        assert crawl._fetch_username() is None

    get.assert_not_called()
