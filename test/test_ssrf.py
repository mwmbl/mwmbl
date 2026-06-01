"""Tests for the SSRF guard and its wiring into fetch() and add_url()."""
import socket

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from mwmbl.crawler import retrieve
from mwmbl.crawler.ssrf import UnsafeURLError, is_safe_host, validate_url

User = get_user_model()


# ---------------------------------------------------------------------------
# Unit: is_safe_host / validate_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "127.0.0.1",
    "127.0.0.2",
    "0.0.0.0",
    "10.0.0.1",
    "192.168.1.1",
    "172.16.0.1",
    "169.254.169.254",   # cloud metadata endpoint
    "::1",
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
])
def test_internal_ip_literals_are_unsafe(host):
    assert is_safe_host(host) is False


def test_public_ip_literal_is_safe():
    assert is_safe_host("93.184.216.34") is True


def test_localhost_resolving_to_loopback_is_unsafe(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, None, None, "", ("127.0.0.1", 0))],
    )
    assert is_safe_host("localhost") is False


def test_hostname_resolving_to_private_ip_is_unsafe(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, None, None, "", ("10.1.2.3", 0))],
    )
    assert is_safe_host("internal.corp") is False


def test_hostname_resolving_to_public_ip_is_safe(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))],
    )
    assert is_safe_host("example.com") is True


def test_resolution_failure_is_unsafe(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert is_safe_host("does-not-resolve.invalid") is False


def test_validate_url_rejects_non_http_scheme():
    with pytest.raises(UnsafeURLError):
        validate_url("file:///etc/passwd")
    with pytest.raises(UnsafeURLError):
        validate_url("ftp://example.com/x")


def test_validate_url_rejects_internal():
    with pytest.raises(UnsafeURLError):
        validate_url("http://127.0.0.1/admin")


# ---------------------------------------------------------------------------
# fetch() integration
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, headers=None, is_redirect=False, body=b"hello"):
        self.status_code = status_code
        self.headers = headers or {}
        self.is_redirect = is_redirect
        self.next = object() if is_redirect else None
        self._body = body

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


def test_fetch_blocks_internal_url_without_request(monkeypatch):
    calls = []
    monkeypatch.setattr(retrieve.requests, "get",
                        lambda *a, **k: calls.append(a) or _FakeResponse(200))
    with pytest.raises(UnsafeURLError):
        retrieve.fetch("http://127.0.0.1/secret")
    assert calls == [], "requests.get must not be called for an internal URL"


def test_fetch_blocks_redirect_to_internal(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        # First (public) hop redirects to an internal address.
        return _FakeResponse(301, headers={"Location": "http://127.0.0.1/"}, is_redirect=True)

    monkeypatch.setattr(retrieve.requests, "get", fake_get)
    with pytest.raises(UnsafeURLError):
        retrieve.fetch("http://93.184.216.34/start")
    assert len(calls) == 1, "must stop after the first hop, before fetching the internal redirect target"


def test_fetch_returns_content_for_public_url(monkeypatch):
    monkeypatch.setattr(retrieve.requests, "get",
                        lambda *a, **k: _FakeResponse(200, body=b"page body"))
    status, content = retrieve.fetch("http://93.184.216.34/page")
    assert status == 200
    assert content == b"page body"


# ---------------------------------------------------------------------------
# add_url view
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_add_url_rejects_internal_url(monkeypatch):
    user = User.objects.create_user(username="ssrfuser", email="ssrf@example.com", password="x")
    EmailAddress.objects.create(user=user, email="ssrf@example.com", verified=True, primary=True)

    # Fail loudly if the view ever reaches the network for an internal URL.
    def boom(*a, **k):
        raise AssertionError("requests.get must not be called for an internal URL")
    monkeypatch.setattr("mwmbl.views.requests.get", boom)

    client = Client()
    client.force_login(user)
    response = client.post(reverse("add_url"), {"new_url": "http://169.254.169.254/", "query": "x"})
    assert response.status_code == 400
