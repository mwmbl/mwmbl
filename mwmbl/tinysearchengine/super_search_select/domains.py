"""URL/host normalization and source-domain matching for Super Search.

Shared by the offline evaluation harnesses (``scripts/super_search_coverage.py``,
``scripts/super_search_eval.py``) that need to decide which Super Search source a
URL belongs to. Matching is by *registrable domain* (``www.``/``m.`` stripped,
last two labels — three for known multi-label suffixes like ``co.uk``); exact-host
string equality is what the NDCG harness scores on, but for "which source contains
this result" the registrable domain is the right granularity.
"""
from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlparse

# Common multi-label public suffixes so registrable-domain folding doesn't collapse
# e.g. bbc.co.uk -> co.uk. Not exhaustive (no full public-suffix list), but covers
# the suffixes that actually appear in the gold set / source shortlist.
_MULTI_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "co.za", "co.in", "co.jp", "co.kr", "com.br", "com.mx",
    "com.cn", "com.tr", "com.sg", "com.hk", "or.jp", "ne.jp",
}


def host_of(url: str) -> str:
    """Lowercase host of a URL, sans userinfo and port. ``""`` if unparseable."""
    try:
        netloc = urlparse(str(url)).netloc.lower()
    except ValueError:
        return ""
    return netloc.split("@")[-1].split(":")[0]


def _strip_www(host: str) -> str:
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


def registrable(host: str) -> str:
    """Registrable domain: strip ``www.``/``m.`` then take the last 2 labels (3 for
    known multi-label suffixes like ``co.uk``)."""
    host = _strip_www(host)
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def source_domain_map() -> dict[str, list[str]]:
    """Registrable source domain -> source names, built from the source registry.

    A single registrable domain can map to several sources (e.g. sibling
    subdomains of the same registrable domain).
    """
    from mwmbl.tinysearchengine.super_search_select.registry import get_registry

    reg_map: dict[str, list[str]] = defaultdict(list)
    for name, meta in get_registry().items():
        reg_map[registrable(meta.domain.lower())].append(name)
    return dict(reg_map)
