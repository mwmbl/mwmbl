"""Tests for provider usage counting and the provider costs admin page."""

from unittest.mock import MagicMock, patch

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from mwmbl import provider_usage, traffic
from mwmbl.indexer.external_cache import external_cache_path
from mwmbl.tinysearchengine import staan
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, TinyIndex
from mwmbl.tinysearchengine.staan import get_staan_results
from mwmbl.utils import utc_today

User = get_user_model()

COSTS_URL = "/admin/provider-costs/"


@pytest.fixture
def redis(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(traffic, "get_redis", lambda: client)
    return client


@pytest.fixture
def counting(redis):
    with override_settings(PROVIDER_USAGE_COUNTING=True):
        yield redis


def _usage_today(redis):
    return provider_usage.read_usage(redis, [utc_today()])[utc_today()]


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

    assert _usage_today(counting)["staan"] == {"calls": 1, "failures": 0}


def test_a_failed_staan_call_is_counted_as_a_call_and_a_failure(counting, staan_cache):
    with patch.object(staan.session, "get", side_effect=TimeoutError):
        get_staan_results("rust async")

    assert _usage_today(counting)["staan"] == {"calls": 1, "failures": 1}


def test_nothing_is_counted_when_counting_is_off(redis):
    with override_settings(PROVIDER_USAGE_COUNTING=False):
        provider_usage.record_provider_call("staan", failed=False)

    assert redis.keys() == []


def test_cost_is_calls_times_the_price_in_pounds(monkeypatch):
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "staan", 4.0)

    assert provider_usage.cost_gbp("staan", 200) == pytest.approx(0.8)


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
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "staan", 3.0)
    counting.hincrby(provider_usage.USAGE_KEY.format(date=utc_today()), "staan:calls", 2000)

    response = staff_client.get(COSTS_URL)

    assert response.status_code == 200
    totals = {total["provider"]: total for total in response.context["totals"]}
    assert totals["staan"]["cost_gbp"] == 6
    assert response.context["total_cost_gbp"] == 6
    assert "&pound;6.00" in response.content.decode()


def test_the_chart_stacks_each_days_providers_and_scales_to_the_busiest_day(staff_client, counting, monkeypatch):
    monkeypatch.setitem(provider_usage.PRICE_PER_1000_QUERIES_GBP, "staan", 5.0)
    counting.hincrby(provider_usage.USAGE_KEY.format(date=utc_today()), "staan:calls", 100)

    response = staff_client.get(COSTS_URL, {"days": 7})

    bars = response.context["chart"]["bars"]
    assert len(bars) == 7
    assert bars[-1]["day"] == utc_today()
    assert [segment["height_px"] for segment in bars[-1]["segments"]] == [220]
    assert bars[0]["height_px"] == 0


def test_the_calls_chart_shows_volume_whatever_the_price(staff_client, counting):
    counting.hincrby(provider_usage.USAGE_KEY.format(date=utc_today()), "staan:calls", 10)

    response = staff_client.get(COSTS_URL, {"metric": "calls"})

    assert response.context["chart"]["bars"][-1]["segments"][0]["height_px"] == 220
