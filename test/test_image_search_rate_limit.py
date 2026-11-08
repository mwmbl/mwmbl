"""Tests for the per-client limit on the image endpoints."""

import pytest
from django.core.cache import cache
from django.test import Client

from mwmbl.imagesearch import pipeline, rate_limit

SEARCH = "/api/v2/image-search/"
TOP = "/api/v2/image-search/top/"


@pytest.fixture(autouse=True)
def limit_of_three(settings, monkeypatch):
    settings.IMAGE_SEARCH_REQUESTS_PER_MINUTE = 3
    cache.clear()
    monkeypatch.setattr(pipeline, "search_images", lambda query, limit: [])
    monkeypatch.setattr(pipeline, "top_image", lambda query: None)
    yield
    cache.clear()


def get_statuses(client: Client, path: str, count: int, **headers) -> list[int]:
    return [client.get(path, {"q": "red panda"}, **headers).status_code for _ in range(count)]


def test_a_client_over_the_limit_gets_a_429_and_the_pipeline_is_not_run(monkeypatch):
    ran = []
    monkeypatch.setattr(pipeline, "search_images", lambda query, limit: ran.append(query) or [])
    client = Client(REMOTE_ADDR="203.0.113.5")
    assert get_statuses(client, SEARCH, 5) == [200, 200, 200, 429, 429]
    assert len(ran) == 3


def test_both_endpoints_share_one_budget():
    client = Client(REMOTE_ADDR="203.0.113.5")
    assert get_statuses(client, SEARCH, 2) == [200, 200]
    assert get_statuses(client, TOP, 2) == [200, 429]


def test_clients_are_limited_independently():
    assert get_statuses(Client(REMOTE_ADDR="203.0.113.5"), SEARCH, 4) == [200, 200, 200, 429]
    assert get_statuses(Client(REMOTE_ADDR="203.0.113.6"), SEARCH, 1) == [200]


def test_the_address_our_proxy_saw_is_the_one_limited():
    # The last X-Forwarded-For entry is the proxy's; earlier ones are the client's own say-so.
    client = Client(REMOTE_ADDR="10.0.0.1")
    first = get_statuses(client, SEARCH, 4, HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.9")
    spoofed = get_statuses(client, SEARCH, 1, HTTP_X_FORWARDED_FOR="198.51.100.77, 203.0.113.9")
    assert first == [200, 200, 200, 429]
    assert spoofed == [429]


def test_ipv6_addresses_in_one_network_share_a_budget():
    first = get_statuses(Client(REMOTE_ADDR="2001:db8:1:2::1"), SEARCH, 3)
    other_host_in_network = get_statuses(Client(REMOTE_ADDR="2001:db8:1:2::ffff"), SEARCH, 1)
    assert first == [200, 200, 200]
    assert other_host_in_network == [429]


def test_the_limit_resets_in_the_next_minute(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: now[0])
    client = Client(REMOTE_ADDR="203.0.113.5")
    assert get_statuses(client, SEARCH, 4) == [200, 200, 200, 429]
    now[0] += rate_limit.WINDOW_SECONDS
    assert get_statuses(client, SEARCH, 1) == [200]


def test_a_window_that_expires_between_add_and_incr_starts_a_new_count(monkeypatch):
    def expired(key):
        raise ValueError(key)

    client = Client(REMOTE_ADDR="203.0.113.5")
    assert get_statuses(client, SEARCH, 1) == [200]
    monkeypatch.setattr(cache, "incr", expired)
    assert get_statuses(client, SEARCH, 1) == [200]
