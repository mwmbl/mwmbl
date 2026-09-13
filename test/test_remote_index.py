"""The client the crawler's index sync and every rankeval script use to read the main index.

Two things are pinned here, both of which were true of api.mwmbl.org's traffic before they
were fixed. It sends a User-Agent that names it, so that its share of the API's traffic is
attributable rather than arriving as an anonymous python-requests. And the server it talks
to comes from the environment, so that an evaluation sweep is not aimed at production by
the mere fact of calling RemoteIndex() with no argument.
"""

import importlib
from unittest.mock import MagicMock, patch

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
