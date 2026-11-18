"""Connecting fediverse servers to Mwmbl Social (mwmbl.social)."""

import pytest
from django.core.cache import cache

from mwmbl import social


@pytest.fixture(autouse=True)
def empty_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.parametrize(
    "entered, expected",
    [
        ("mastodon.example", ("https://mastodon.example", "mastodon.example")),
        ("https://Mastodon.Example/about", ("https://mastodon.example", "mastodon.example")),
        ("https://m.example:8443", ("https://m.example:8443", "m.example:8443")),
        # Other spellings of one server get the same host, so they share its rate limit.
        ("https://m.example:443/", ("https://m.example", "m.example")),
        ("m.example.", ("https://m.example", "m.example")),
        ("münchen.social", ("https://xn--mnchen-3ya.social", "xn--mnchen-3ya.social")),
    ],
)
def test_a_server_address_is_normalised_to_https_and_its_host(entered, expected):
    assert social.server_url_and_host(entered) == expected


@pytest.mark.parametrize(
    "entered",
    [
        "http://mastodon.example",
        "https://",
        "https://me:pw@mastodon.example",
        # Mwmbl Social would fetch these from inside our network.
        "https://127.0.0.1",
        "https://0x7f.1",
        "https://[::1]",
        "localhost",
        "https://2130706433",
    ],
)
def test_unusable_server_addresses_are_refused(entered):
    with pytest.raises(ValueError):
        social.server_url_and_host(entered)


def test_the_limits_hold_on_redis(redis_cache, settings):
    settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY = 2

    assert [social.take_user_attempt(1) for _ in range(3)] == [True, True, False]
    assert [social.take_host_attempt("mastodon.example") for _ in range(2)] == [True, False]
    assert 0 < redis_cache.ttl(cache.make_key("social:connect:host:mastodon.example")) <= 60 * 60
