import itertools
import socket
import threading
from copy import deepcopy
from urllib.parse import quote

import httpx
import pytest
from ninja import NinjaAPI
from ninja.testing import TestClient

from mwmbl.crawler.retrieve import RobotsUnavailable
from mwmbl.widgets import api as widgets_api
from mwmbl.widgets.fetch import FetchError, fetch_json
from mwmbl.widgets.spec import WidgetSpec

API_NUMBERS = itertools.count()
PUBLIC_IP = "93.184.216.34"
SPEC = {
    "name": "weather",
    "site": "example.com",
    "source_name": "Example Weather",
    "page_url": "https://example.com/forecast?place={place}&days={days}",
    "api_url": "https://api.example.com/v1?place={place}&days={days}",
    "api_host": "api.example.com",
    "inputs": [
        {"name": "place", "type": "text"},
        {"name": "days", "type": "number", "pattern": r"(\d+) days", "default": "3"},
    ],
    "outputs": [
        {"label": "Temperature", "path": "current.temp", "unit": "C"},
        {"label": "Next", "path": "daily[1]"},
        {"label": "Nested", "path": "current"},
    ],
    "trigger_word": "weather",
    "trigger_tokens": ["weather"],
    "cache_ttl_seconds": 600,
}
PAGE = "https://example.com/forecast?place=York&days=2"
ANSWER = {"current": {"temp": 11.5}, "daily": ["a", "<b>x</b>\n"]}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host != "api.example.com":
            return real_getaddrinfo(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def robots(mocker):
    return mocker.patch.object(widgets_api, "robots_allowed", return_value=True)


@pytest.fixture
def client(redis_cache):
    api = NinjaAPI(urls_namespace=f"widgets-{next(API_NUMBERS)}")
    api.add_router("/widgets/", widgets_api.widgets_router([WidgetSpec.model_validate(deepcopy(SPEC))]))
    return TestClient(api)


def get(client, url=PAGE, q=""):
    return client.get(f"/widgets/?url={quote(url, safe='')}&q={quote(q)}", headers={"X-Forwarded-For": "1.2.3.4"})


def test_returns_plain_text_fields_and_caches(client, httpx_mock):
    httpx_mock.add_response(url=f"https://{PUBLIC_IP}/v1?place=York&days=2", json=ANSWER)
    first = get(client).json()
    second = get(client).json()  # a second upstream call would fail: only one response is registered
    assert first == second
    assert first["fields"] == [
        {"label": "Temperature", "value": "11.5", "unit": "C"},
        {"label": "Next", "value": "<b>x</b> ", "unit": ""},
    ]
    assert first["share_url"] == PAGE and first["display"] == "fields"
    assert first["inputs"][1] == {"name": "days", "type": "number", "label": "", "value": "2"}


def test_unknown_url_is_404(client):
    assert get(client, "https://example.com/other").status_code == 404


def test_robots_block_is_404(client, robots):
    robots.return_value = False
    assert get(client).status_code == 404
    robots.side_effect = RobotsUnavailable
    assert get(client).status_code == 404


def test_inputs_come_from_url_then_query_then_default(client, httpx_mock):
    httpx_mock.add_response(url=f"https://{PUBLIC_IP}/v1?place=New%20York&days=5", json=ANSWER)
    page = "https://example.com/forecast?place=New%20York&days={days}"
    assert get(client, page.format(days="5"), q="7 days").status_code == 200


@pytest.mark.parametrize(
    "page", ["https://example.com/forecast?place=York&days=1e9", "https://example.com/forecast?place=%00&days=2"]
)
def test_invalid_input_is_422(client, page):
    assert get(client, page).status_code == 422


def test_input_is_percent_encoded(client, httpx_mock):
    httpx_mock.add_response(url=f"https://{PUBLIC_IP}/v1?place=a%26days%3D9%2F..%2F%23%40evil.com&days=2", json=ANSWER)
    page = "https://example.com/forecast?place=a%26days%3D9%2F..%2F%23%40evil.com&days=2"
    assert get(client, page).status_code == 200


def test_upstream_failure_is_502_and_releases_lock(client, httpx_mock, redis_cache):
    httpx_mock.add_response(status_code=500)
    assert get(client).status_code == 502
    assert not redis_cache.keys("widgets:answer:*:lock")


def test_per_ip_rate_limit(client, httpx_mock, settings):
    settings.WIDGET_RATE_LIMIT_PER_MINUTE = 2
    httpx_mock.add_response(json=ANSWER)
    assert [get(client).status_code for _ in range(3)] == [200, 200, 429]


def test_concurrent_misses_share_one_upstream_call(client, httpx_mock):
    httpx_mock.add_response(json=ANSWER)
    statuses = []
    threads = [threading.Thread(target=lambda: statuses.append(get(client).status_code)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert statuses == [200, 200, 200]
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "::ffff:10.0.0.1"])
def test_fetch_refuses_internal_addresses(monkeypatch, address):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    )
    with pytest.raises(FetchError):
        fetch_json("https://api.example.com/v1", "api.example.com")


def test_fetch_refuses_one_internal_address_among_public_ones(monkeypatch):
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in (PUBLIC_IP, "10.0.0.5")]
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: infos)
    with pytest.raises(FetchError):
        fetch_json("https://api.example.com/v1", "api.example.com")


@pytest.mark.parametrize(
    "url", ["http://api.example.com/v1", "https://other.example.com/v1", "https://api.example.com@evil.com/v1"]
)
def test_fetch_refuses_other_urls(url):
    with pytest.raises(FetchError):
        fetch_json(url, "api.example.com")


def test_fetch_connects_to_the_checked_address(httpx_mock):
    httpx_mock.add_response(json={"a": 1})
    assert fetch_json("https://api.example.com/v1?x=1", "api.example.com") == {"a": 1}
    request = httpx_mock.get_requests()[0]
    assert request.url.host == PUBLIC_IP and request.headers["host"] == "api.example.com"
    assert request.extensions["sni_hostname"] == "api.example.com"


@pytest.mark.parametrize(
    "response",
    [
        {"status_code": 302, "headers": {"Location": "https://10.0.0.1/"}},
        {"content": b"x" * 70_000},
        {"content": b"not json"},
    ],
)
def test_fetch_refuses_redirects_oversize_and_non_json(httpx_mock, response):
    httpx_mock.add_response(**response)
    with pytest.raises(FetchError):
        fetch_json("https://api.example.com/v1", "api.example.com")
    assert len(httpx_mock.get_requests()) == 1


def test_fetch_timeout_is_an_error(httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError):
        fetch_json("https://api.example.com/v1", "api.example.com")
