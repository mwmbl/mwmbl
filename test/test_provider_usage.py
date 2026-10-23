"""Tests for provider usage counting and the provider costs admin page."""

from unittest.mock import MagicMock, Mock, patch

import fakeredis
import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings

from mwmbl import pricing, provider_usage, traffic
from mwmbl.indexer.external_cache import external_cache_path
from mwmbl.tinysearchengine import staan
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, TinyIndex
from mwmbl.tinysearchengine.staan import get_staan_results
from mwmbl.utils import utc_today

User = get_user_model()

COSTS_URL = "/admin/provider-costs/"
IMAGES_URL = "/api/v1/images/search"

BRAVE_RESPONSE = {
    "results": [
        {
            "title": "A mountain",
            "url": "https://example.com/mountain",
            "source": "example.com",
            "thumbnail": {"src": "https://imgs.search.brave.com/thumb"},
            "properties": {"url": "https://example.com/mountain.jpg"},
        }
    ]
}


@pytest.fixture
def redis(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(traffic, "get_redis", lambda: client)
    return client


@pytest.fixture
def counting(redis):
    with override_settings(PROVIDER_USAGE_COUNTING=True):
        yield redis


@pytest.fixture(autouse=True)
def empty_cache():
    cache.clear()
    yield
    cache.clear()


def _usage_today(redis):
    return provider_usage.read_usage(redis, [utc_today()])[utc_today()]


def _brave_responds(status_code=200):
    response = Mock(status_code=status_code, headers={})
    response.json.return_value = BRAVE_RESPONSE
    return patch("mwmbl.image_search.httpx.get", return_value=response)


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_repeated_brave_image_search_counts_a_cache_hit_not_a_call(counting):
    with _brave_responds():
        Client().get(IMAGES_URL, {"q": "mountain"})
        Client().get(IMAGES_URL, {"q": "mountain"})

    assert _usage_today(counting)["brave_images"] == {"calls": 1, "failures": 0, "cache_hits": 1}


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_brave_image_search_counts_one_call(counting):
    with _brave_responds():
        Client().get(IMAGES_URL, {"q": "mountain"})

    assert _usage_today(counting)["brave_images"] == {"calls": 1, "failures": 0, "cache_hits": 0}


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_rate_limited_brave_call_is_counted_as_a_failed_call(counting):
    with _brave_responds(429):
        Client().get(IMAGES_URL, {"q": "mountain"})

    assert _usage_today(counting)["brave_images"] == {"calls": 1, "failures": 1, "cache_hits": 0}


@override_settings(BRAVE_SEARCH_API_KEY="test-key")
def test_a_brave_call_that_times_out_is_counted_and_is_a_502(counting):
    with patch("mwmbl.image_search.httpx.get", side_effect=httpx.ReadTimeout("slow")):
        response = Client().get(IMAGES_URL, {"q": "mountain"})

    assert response.status_code == 502
    assert _usage_today(counting)["brave_images"] == {"calls": 1, "failures": 1, "cache_hits": 0}


@override_settings(BRAVE_SEARCH_API_KEY="")
def test_a_brave_search_that_is_never_sent_is_not_counted(counting):
    Client().get(IMAGES_URL, {"q": "mountain"})

    assert _usage_today(counting)["brave_images"] == {"calls": 0, "failures": 0, "cache_hits": 0}


@pytest.fixture
def staan_cache(tmp_path):
    with override_settings(
        DATA_PATH=str(tmp_path),
        EXTERNAL_CACHE_INDEX_NAME="external-cache.tinysearch",
        EXTERNAL_CACHE_NUM_PAGES=8,
        EXTERNAL_CACHE_ENABLED=True,
        STAAN_SEARCH_API_KEY="test-key",
    ):
        TinyIndex.create(item_factory=Document, index_path=str(external_cache_path()), num_pages=8, page_size=PAGE_SIZE)
        yield


def test_only_a_staan_cache_miss_counts_as_a_call(counting, staan_cache):
    payload = {"web": {"results": [{"url": "https://tokio.rs/", "title": "Tokio", "snippet": "x"}]}}
    with patch.object(staan.session, "get", return_value=MagicMock(**{"json.return_value": payload})):
        get_staan_results("rust async")
        get_staan_results("rust async")

    assert _usage_today(counting)["staan"] == {"calls": 1, "failures": 0, "cache_hits": 1}


def test_a_failed_staan_call_is_counted_as_a_call_and_a_failure(counting, staan_cache):
    with patch.object(staan.session, "get", side_effect=TimeoutError):
        get_staan_results("rust async")

    assert _usage_today(counting)["staan"] == {"calls": 1, "failures": 1, "cache_hits": 0}


def test_nothing_is_counted_when_counting_is_off(redis):
    with override_settings(PROVIDER_USAGE_COUNTING=False):
        provider_usage.record_provider_call("staan")

    assert redis.keys() == []


def test_cost_is_calls_times_the_price_in_pounds(monkeypatch):
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "brave_images", 4.0)

    assert provider_usage.cost_gbp("brave_images", 200) == pytest.approx(0.8)


def test_provider_prices_are_converted_to_pounds_from_the_currency_they_bill_in():
    assert provider_usage.PRICE_PER_1000_QUERIES_GBP["brave_images"] == pytest.approx(5.0 * pricing.GBP_PER_USD)
    assert provider_usage.PRICE_PER_1000_QUERIES_GBP["staan"] == pytest.approx(2.0 * pricing.GBP_PER_EUR)


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user(username="staff_member_1", password="correctpassword", is_staff=True)
    client.force_login(user)
    return client


def test_non_staff_are_redirected_to_login_from_the_costs_page(client, db):
    response = client.get(COSTS_URL)

    assert response.status_code == 302
    assert "/admin/login/" in response.url


def test_the_costs_page_prices_each_provider_and_totals_them(staff_client, counting, monkeypatch):
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "brave_images", 5.0)
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "staan", 3.0)
    counting.hincrby(provider_usage.USAGE_KEY.format(date=utc_today()), "brave_images:calls", 1000)
    counting.hincrby(provider_usage.USAGE_KEY.format(date=utc_today()), "staan:calls", 2000)

    response = staff_client.get(COSTS_URL)

    assert response.status_code == 200
    totals = {total["provider"]: total for total in response.context["totals"]}
    assert totals["brave_images"]["cost_gbp"] == 5
    assert totals["staan"]["cost_gbp"] == 6
    assert response.context["total_cost_gbp"] == 11
    assert "&pound;11.00" in response.content.decode()


def test_the_costs_page_shows_what_caching_saved(staff_client, counting, monkeypatch):
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "brave_images", 5.0)
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "staan", 2.0)
    key = provider_usage.USAGE_KEY.format(date=utc_today())
    counting.hincrby(key, "brave_images:calls", 1000)
    counting.hincrby(key, "brave_images:cache_hits", 3000)
    counting.hincrby(key, "staan:calls", 1000)
    counting.hincrby(key, "staan:cache_hits", 1000)

    response = staff_client.get(COSTS_URL)

    totals = {total["provider"]: total for total in response.context["totals"]}
    assert totals["brave_images"]["saved_gbp"] == 15
    assert totals["brave_images"]["hit_rate_percent"] == 75
    assert totals["staan"]["saved_gbp"] == 2
    assert response.context["total_saved_gbp"] == 17
    assert response.context["total_hit_rate_percent"] == pytest.approx(100 * 4000 / 6000)
    assert "Caching saved <strong>&pound;17.00</strong>" in response.content.decode()


def test_the_hit_rate_is_blank_when_nothing_was_looked_up(staff_client, counting):
    response = staff_client.get(COSTS_URL)

    assert response.context["total_hit_rate_percent"] is None
    assert "No lookups in this window." in response.content.decode()


def test_the_chart_stacks_each_days_providers_and_scales_to_the_busiest_day(staff_client, counting, monkeypatch):
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "brave_images", 5.0)
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "staan", 5.0)
    counting.hincrby(provider_usage.USAGE_KEY.format(date=utc_today()), "brave_images:calls", 100)
    counting.hincrby(provider_usage.USAGE_KEY.format(date=utc_today()), "staan:calls", 100)

    response = staff_client.get(COSTS_URL, {"days": 7})

    bars = response.context["chart"]["bars"]
    assert len(bars) == 7
    assert bars[-1]["day"] == utc_today()
    assert [segment["height_px"] for segment in bars[-1]["segments"]] == [110, 110]
    assert bars[0]["height_px"] == 0


def test_the_calls_chart_shows_volume_whatever_the_price(staff_client, counting):
    counting.hincrby(provider_usage.USAGE_KEY.format(date=utc_today()), "staan:calls", 10)

    response = staff_client.get(COSTS_URL, {"metric": "calls"})

    assert response.context["chart"]["bars"][-1]["segments"][1]["height_px"] == 220
