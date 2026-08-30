"""
Tests for the Staan search adapter.
"""

from unittest.mock import patch, MagicMock

from mwmbl.tinysearchengine.staan import get_staan_results


def test_get_staan_results_success():
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "web": {
            "results": [
                {"url": "https://example.com", "title": "Example", "snippet": "An example site"},
                {"url": "https://other.com", "title": "Other", "description": "Fallback field"},
            ]
        }
    }

    with patch("mwmbl.tinysearchengine.staan.settings") as mock_settings, \
            patch("mwmbl.tinysearchengine.staan.requests.get", return_value=mock_response) as mock_get:
        mock_settings.STAAN_SEARCH_API_KEY = "test-key"

        results = get_staan_results("open source llms")

    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert kwargs["params"] == {"q": "open source llms", "market": "en-us"}
    assert kwargs["headers"] == {"Authorization": "Bearer test-key"}

    assert results == [
        {
            "url": "https://example.com",
            "title": [{"value": "Example", "is_bold": False}],
            "extract": [{"value": "An example site", "is_bold": False}],
            "source": "staan",
        },
        {
            "url": "https://other.com",
            "title": [{"value": "Other", "is_bold": False}],
            "extract": [{"value": "Fallback field", "is_bold": False}],
            "source": "staan",
        },
    ]


def test_get_staan_results_no_api_key():
    with patch("mwmbl.tinysearchengine.staan.settings") as mock_settings:
        mock_settings.STAAN_SEARCH_API_KEY = ""
        results = get_staan_results("open source llms")

    assert results == []


def test_get_staan_results_request_failure():
    with patch("mwmbl.tinysearchengine.staan.settings") as mock_settings, \
            patch("mwmbl.tinysearchengine.staan.requests.get", side_effect=Exception("boom")):
        mock_settings.STAAN_SEARCH_API_KEY = "test-key"
        results = get_staan_results("open source llms")

    assert results == []
