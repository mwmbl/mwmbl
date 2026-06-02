"""SSRF (server-side request forgery) protection for outbound URL fetches.

Any code that fetches a URL whose host is influenced by external data (crawled
pages, search-result links, user submissions) should validate it here first.
We resolve the host to its IP address(es) and refuse to fetch anything that
points at a private, loopback, link-local, reserved or otherwise internal
address — most importantly the cloud metadata endpoint (169.254.169.254).

This does not pin the connection to the resolved IP, so a small DNS-rebinding
(time-of-check/time-of-use) window remains; closing that would require custom
transport adapters and is left as a future hardening step. Callers that follow
HTTP redirects must re-validate each hop, since a public URL can 3xx to an
internal one.
"""
import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(ValueError):
    """Raised when a URL resolves to an address we refuse to fetch."""


def _ip_is_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only for ordinary, routable public addresses."""
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) so the v4 checks apply.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_safe_host(host: str) -> bool:
    """Return True if every address ``host`` resolves to is a public address.

    ``host`` may be an IP literal or a hostname. Hostnames are resolved with
    ``getaddrinfo``; resolution failure is treated as unsafe.
    """
    if not host:
        return False

    # IP literal? Check it directly without a DNS lookup.
    try:
        return _ip_is_safe(ipaddress.ip_address(host))
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False

    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not _ip_is_safe(ip):
            return False
    return True


def validate_url(url: str) -> None:
    """Raise :class:`UnsafeURLError` if ``url`` is not safe to fetch."""
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise UnsafeURLError(f"Could not parse URL: {url}") from e

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Refusing to fetch non-http(s) URL: {url}")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError(f"URL has no host: {url}")

    if not is_safe_host(host):
        raise UnsafeURLError(f"Refusing to fetch internal/private address: {url}")
