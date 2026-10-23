from unittest.mock import Mock, patch

import pytest
from django.test import Client, override_settings

URL = "/api/v1/images/search"

BRAVE_RESPONSE = {
    "results": [
        {
            "title": "A mountain",
            "url": "https://example.com/mountain",
            "source": "example.com",
            "thumbnail": {"src": "https://imgs.search.brave.com/thumb"},
            "properties": {"url": "https://example.com/mountain.jpg", "width": 800, "height": 600},
        }
    ]
}


@pytest.fixture
def brave_get():
    response = Mock(status_code=200)
    response.json.return_value = BRAVE_RESPONSE
    with patch("mwmbl.image_search.httpx.get", return_value=response) as get:
        yield get


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_image_search_always_requests_strict_safe_search(brave_get):
    response = Client().get(URL, {"q": "mountain", "count": 5})

    assert response.status_code == 200
    assert response.json()["results"][0]["thumbnail_url"] == "https://imgs.search.brave.com/thumb"
    call = brave_get.call_args
    assert call.kwargs["params"] == {"q": "mountain", "count": 5, "safesearch": "strict"}
    assert call.kwargs["headers"]["X-Subscription-Token"] == "test-key"


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_image_search_cannot_turn_safe_search_off(brave_get):
    Client().get(URL, {"q": "mountain", "safesearch": "off"})

    assert brave_get.call_args.kwargs["params"]["safesearch"] == "strict"


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_image_search_passes_on_brave_rate_limiting_as_a_429(brave_get):
    brave_get.return_value.status_code = 429

    response = Client().get(URL, {"q": "mountain"})

    assert response.status_code == 429


@override_settings(BRAVE_SEARCH_API_KEY="")
def test_image_search_without_a_key_is_a_503(brave_get):
    response = Client().get(URL, {"q": "mountain"})

    assert response.status_code == 503
    brave_get.assert_not_called()
