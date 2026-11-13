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

import httpx
from django.conf import settings

from mwmbl.crawler.retrieve import USER_AGENT
from mwmbl.crawler.ssrf import ip_is_safe

HTTPS_PORT = 443


class FetchError(Exception):
    """The provider could not give a usable answer: unsafe host, error status, too slow, too big, not JSON."""


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
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            # The name the certificate is checked against, since the URL has the address.
            with client.stream(
                "GET", pinned_url, headers=headers, extensions={"sni_hostname": allowed_host}
            ) as response:
                if response.status_code != 200:
                    raise FetchError(f"{allowed_host} answered {response.status_code}")
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > settings.WIDGET_MAX_RESPONSE_BYTES:
                        raise FetchError(f"{allowed_host} answered with more than the size limit")
                    if time.monotonic() > deadline:
                        raise FetchError(f"{allowed_host} was too slow")
    except httpx.HTTPError as e:
        raise FetchError(f"{allowed_host} request failed: {e!r}") from e
    try:
        return json.loads(body)
    except ValueError as e:
        raise FetchError(f"{allowed_host} did not answer with JSON") from e
