import threading
import time
from unittest.mock import Mock, patch

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from mwmbl import image_search
from mwmbl.image_search import CACHE_KEY, CIRCUIT_KEY, LOCK_KEY, LOCK_WAIT_SECONDS, MAX_COUNT, _cooldown_seconds
from mwmbl.indexer.external_cache import external_cache_term

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


@pytest.fixture(autouse=True)
def empty_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def brave_get():
    response = Mock(status_code=200, headers={})
    response.json.return_value = BRAVE_RESPONSE
    with patch("mwmbl.image_search.httpx.get", return_value=response) as get:
        yield get


def _lock_key(query):
    return LOCK_KEY.format(term=external_cache_term(query))


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_simultaneous_searches_for_one_query_make_one_brave_call(brave_get):
    def slow_brave(*args, **kwargs):
        time.sleep(0.3)
        return brave_get.return_value

    brave_get.side_effect = slow_brave
    statuses = []

    def search():
        statuses.append(Client().get(URL, {"q": "mountain"}).status_code)

    threads = [threading.Thread(target=search) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [200] * 5
    assert brave_get.call_count == 1


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_waiting_search_is_answered_by_the_fetch_it_waited_on(brave_get, monkeypatch):
    cache.set(_lock_key("mountain"), 1, timeout=30)
    other_workers_result = {
        "title": "From the other worker",
        "page_url": "https://example.com/",
        "source": "example.com",
        "thumbnail_url": "https://imgs.search.brave.com/other",
        "image_url": "https://example.com/other.jpg",
        "width": None,
        "height": None,
    }
    cache_key = CACHE_KEY.format(term=external_cache_term("mountain"))
    monkeypatch.setattr(image_search, "sleep", lambda seconds: cache.set(cache_key, [other_workers_result], 60))

    response = Client().get(URL, {"q": "mountain"})

    assert response.json() == {"results": [other_workers_result]}
    brave_get.assert_not_called()


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_lock_held_too_long_does_not_stall_the_search(brave_get, monkeypatch):
    cache.set(_lock_key("mountain"), 1, timeout=30)
    clock = [0.0]
    monkeypatch.setattr(image_search, "monotonic", lambda: clock[0])
    monkeypatch.setattr(image_search, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    response = Client().get(URL, {"q": "mountain"})

    assert response.status_code == 200
    assert brave_get.call_count == 1
    assert clock[0] >= LOCK_WAIT_SECONDS
    # Someone else's lock is theirs to release.
    assert cache.get(_lock_key("mountain")) == 1


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_the_lock_is_released_after_a_fetch_succeeds_or_fails(brave_get):
    Client().get(URL, {"q": "mountain"})
    brave_get.return_value.status_code = 500
    Client().get(URL, {"q": "valley"})

    assert cache.get(_lock_key("mountain")) is None
    assert cache.get(_lock_key("valley")) is None


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_image_search_always_requests_strict_safe_search(brave_get):
    response = Client().get(URL, {"q": "mountain", "count": 5})

    assert response.status_code == 200
    assert response.json()["results"][0]["thumbnail_url"] == "https://imgs.search.brave.com/thumb"
    call = brave_get.call_args
    assert call.kwargs["params"] == {"q": "mountain", "count": MAX_COUNT, "safesearch": "strict"}
    assert call.kwargs["headers"]["X-Subscription-Token"] == "test-key"


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_repeated_query_is_answered_from_the_cache(brave_get):
    first = Client().get(URL, {"q": "mountain"})
    second = Client().get(URL, {"q": "  Mountain "})

    assert brave_get.call_count == 1
    assert second.json() == first.json()


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_one_cached_fetch_answers_every_count(brave_get):
    brave_get.return_value.json.return_value = {"results": BRAVE_RESPONSE["results"] * 10}

    small = Client().get(URL, {"q": "mountain", "count": 3})
    large = Client().get(URL, {"q": "mountain", "count": 8})

    assert brave_get.call_count == 1
    assert len(small.json()["results"]) == 3
    assert len(large.json()["results"]) == 8


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_the_cache_is_keyed_on_a_hash_of_the_query_not_its_text(brave_get):
    Client().get(URL, {"q": "a private query"})

    assert cache.get(CACHE_KEY.format(term=external_cache_term("a private query"))) is not None
    assert cache.get(CACHE_KEY.format(term="a private query")) is None


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_query_with_no_images_is_cached_too(brave_get):
    brave_get.return_value.json.return_value = {"results": []}

    Client().get(URL, {"q": "nothing"})
    response = Client().get(URL, {"q": "nothing"})

    assert brave_get.call_count == 1
    assert response.json() == {"results": []}


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_failed_fetch_is_not_cached(brave_get):
    brave_get.return_value.status_code = 500
    Client().get(URL, {"q": "mountain"})
    brave_get.return_value.status_code = 200

    response = Client().get(URL, {"q": "mountain"})

    assert response.status_code == 200
    assert brave_get.call_count == 2


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_results_may_be_kept_briefly_by_the_browser(brave_get):
    response = Client().get(URL, {"q": "mountain"})

    assert response["Cache-Control"] == "private, max-age=300"


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_image_search_cannot_turn_safe_search_off(brave_get):
    Client().get(URL, {"q": "mountain", "safesearch": "off"})

    assert brave_get.call_args.kwargs["params"]["safesearch"] == "strict"


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_image_search_passes_on_brave_rate_limiting_as_a_429(brave_get):
    brave_get.return_value.status_code = 429

    response = Client().get(URL, {"q": "mountain"})

    assert response.status_code == 429


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_rate_limit_pauses_brave_calls_for_as_long_as_brave_asks(brave_get):
    brave_get.return_value.status_code = 429
    brave_get.return_value.headers = {"Retry-After": "30"}

    Client().get(URL, {"q": "mountain"})
    response = Client().get(URL, {"q": "another query"})

    assert response.status_code == 429
    assert brave_get.call_count == 1
    assert cache.get(CIRCUIT_KEY) == 1


@pytest.mark.parametrize("retry_after, seconds", [("30", 30), ("", 10), ("Wed, 21 Oct 2026", 10), ("86400", 300)])
def test_the_pause_follows_retry_after_within_bounds(retry_after, seconds):
    assert _cooldown_seconds(Mock(headers={"Retry-After": retry_after})) == seconds


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_cached_query_is_still_answered_while_brave_is_paused(brave_get):
    Client().get(URL, {"q": "mountain"})
    cache.set(CIRCUIT_KEY, 1, timeout=30)

    response = Client().get(URL, {"q": "mountain"})

    assert response.status_code == 200
    assert brave_get.call_count == 1


@override_settings(BRAVE_SEARCH_API_KEY="")
def test_image_search_without_a_key_is_a_503(brave_get):
    response = Client().get(URL, {"q": "mountain"})

    assert response.status_code == 503
    brave_get.assert_not_called()
