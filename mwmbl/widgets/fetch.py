"""The one outbound call a widget makes: an https GET to a spec's API host.

The host is resolved here and the connection made to the address that was checked, so a DNS answer
that changes between the check and the connection (rebinding) cannot reach an internal address. TLS
still verifies the certificate against the host name. Redirects are not followed.
"""

import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit

import httpcore
import httpx
from django.conf import settings

from mwmbl.crawler.retrieve import USER_AGENT
from mwmbl.crawler.ssrf import ip_is_safe

HTTPS_PORT = 443


class FetchError(Exception):
    """The provider could not give a usable answer: unsafe host, error status, too slow, too big, not JSON."""


class _DeadlineStream(httpcore.NetworkStream):
    """A connection whose every read and write must finish by the deadline: the library's timeout is per operation."""

    def __init__(self, stream, deadline: float):
        self._stream = stream
        self._deadline = deadline

    def _left(self, timeout):
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise httpcore.ReadTimeout("deadline passed")
        return left if timeout is None else min(timeout, left)

    def read(self, max_bytes, timeout=None):
        return self._stream.read(max_bytes, timeout=self._left(timeout))

    def write(self, buffer, timeout=None):
        self._stream.write(buffer, timeout=self._left(timeout))

    def close(self):
        self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        stream = self._stream.start_tls(ssl_context, server_hostname=server_hostname, timeout=self._left(timeout))
        return _DeadlineStream(stream, self._deadline)

    def get_extra_info(self, info):
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore.SyncBackend):
    def __init__(self, deadline: float):
        self._deadline = deadline

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise httpcore.ConnectTimeout("deadline passed")
        timeout = left if timeout is None else min(timeout, left)
        stream = super().connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        return _DeadlineStream(stream, self._deadline)


def public_address(host: str) -> str:
    """An address of the host, which must have only public ones."""
    try:
        infos = socket.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"{host} did not resolve") from e
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or not all(ip_is_safe(address) for address in addresses):
        raise FetchError(f"{host} does not resolve to public addresses only")
    return str(addresses[0])


def fetch_json(url: str, allowed_host: str):
    """The JSON an https GET of the URL answers with, if it is on the allowed host."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != allowed_host:
        raise FetchError(f"{url} is not an https URL on {allowed_host}")
    address = public_address(allowed_host)
    ip_host = f"[{address}]" if ":" in address else address
    pinned_url = parts._replace(netloc=ip_host).geturl()
    headers = {
        "Host": allowed_host,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    timeout = settings.WIDGET_FETCH_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    body = b""
    try:
        # trust_env=False: an environment proxy would resolve the host again itself, defeating the pinning.
        transport = httpx.HTTPTransport()
        transport._pool._network_backend = _DeadlineBackend(deadline)  # the deadline covers the whole exchange
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
            # The name the certificate is checked against, since the URL has the address.
            with client.stream(
                "GET", pinned_url, headers=headers, extensions={"sni_hostname": allowed_host}
            ) as response:
                if response.status_code != 200:
                    raise FetchError(f"{allowed_host} answered {response.status_code}")
                # Raw, not decoded: a compressed answer must not expand past the cap.
                for chunk in response.iter_raw():
                    body += chunk
                    if len(body) > settings.WIDGET_MAX_RESPONSE_BYTES:
                        raise FetchError(f"{allowed_host} answered with more than the size limit")
                    if time.monotonic() > deadline:
                        raise FetchError(f"{allowed_host} was too slow")
    except httpx.HTTPError as e:
        raise FetchError(f"{allowed_host} request failed: {e!r}") from e
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as e:  # RecursionError: deeply nested JSON
        raise FetchError(f"{allowed_host} did not answer with JSON") from e
