"""Mwmbl Social's admin API client, server addresses and sign-up rate limits (mwmbl.social), with
the admin API faked at the HTTP boundary by pytest-httpx."""

import json

import pytest
from django.core.cache import cache

from mwmbl import social

API = "https://social.test/api/v1/admin/registrations"


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
        "mwmbl-social.default.svc.cluster.local",
        "https://postgres.internal:5432",
        # Too long for SocialRegistration.server_url once in punycode.
        "münchen." * 14 + "social",
    ],
)
def test_unusable_server_addresses_are_refused(entered):
    with pytest.raises(ValueError):
        social.server_url_and_host(entered)


def test_each_user_has_a_daily_limit_and_is_told_when_it_resets(settings):
    settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY = 2

    waits = [social.user_attempt_wait(1) for _ in range(3)]

    assert waits[:2] == [None, None]
    assert 0 < waits[2] <= social.DAY_SECONDS
    assert social.user_attempt_wait(2) is None


def test_the_limits_hold_on_redis(redis_cache, settings):
    settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY = 2

    assert [social.user_attempt_wait(1) is None for _ in range(3)] == [True, True, False]
    assert social.host_attempt_wait("mastodon.example") is None
    assert 0 < social.host_attempt_wait("mastodon.example") <= 60 * 60
    assert 0 < redis_cache.ttl(cache.make_key("social:connect:host:mastodon.example")) <= 60 * 60


def test_registering_sends_the_address_with_the_admin_token(httpx_mock):
    httpx_mock.add_response(method="POST", url=API, status_code=201, json={"id": 4})

    assert social.register("https://mastodon.example") == {"id": 4}
    sent = httpx_mock.get_request()
    assert sent.headers["Authorization"] == "Bearer test-admin-token"
    assert json.loads(sent.content) == {"server_url": "https://mastodon.example"}


@pytest.mark.parametrize("status", [400, 409, 502])
def test_refusals_about_the_server_are_told_apart_from_misconfiguration(httpx_mock, status):
    httpx_mock.add_response(method="POST", url=API, status_code=status, json={"error": "no"})

    with pytest.raises(social.RegistrationRefused) as refused:
        social.register("https://mastodon.example")
    assert refused.value.status == status


def test_registrations_are_read_by_id_and_a_replaced_one_revokes_quietly(httpx_mock):
    httpx_mock.add_response(method="GET", url=API, json=[{"id": 4, "status": "active"}])
    httpx_mock.add_response(method="DELETE", url=f"{API}/4", status_code=404, json={})

    assert social.registrations_by_id() == {4: {"id": 4, "status": "active"}}
    social.revoke(4)
