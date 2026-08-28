"""Crawl a submitted domain so a moderator - and the model - can see what is actually there.

Three pages, not one: a homepage alone is often a splash screen, and the reject reasons
moderators write ("obtrusive ads", "doesn't render anything/no links", "is search UI and bot
detection") are judgements about a site, not a page. The same three pages are shown in the
moderator's detail panel and fed to the model, so what a moderator sees and what the model
judged can never disagree.

This reuses mwmbl.crawler.retrieve.crawl_url, which already does robots.txt with Redis
caching, SSRF validation, a 3 second timeout, a 1 MB cap and justext extraction with Open
Graph fallbacks. Super Search already calls it from inside the server process, so nothing
here is a new capability - it is the existing crawler pointed at one domain.
"""
from __future__ import annotations

import time
from logging import getLogger
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from mwmbl.crawler.env_vars import CRAWL_DELAY_SECONDS
from mwmbl.crawler.retrieve import crawl_url
from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist
from mwmbl.models import DomainEvidence
from mwmbl.moderation import rules

logger = getLogger(__name__)

# Paths that tell you nothing about whether a site is worth crawling.
BORING_PATH_WORDS = ("privacy", "terms", "cookie", "legal", "login", "signin", "sign-in",
                     "register", "cart", "checkout", "contact", "rss", "feed")

# Markers counted as signals for the moderator. Not model features - they are far too crude
# for that - but "we found 4 ad-network scripts" is a useful line in an evidence list, and
# moderators do reject for "obtrusive ads" and "paywall".
AD_MARKERS = ("googlesyndication", "doubleclick", "adsbygoogle", "adnxs", "taboola",
              "outbrain", "criteo", "media.net")
PAYWALL_MARKERS = ("paywall", "subscribe to continue", "subscribers only",
                   "create a free account to")


def crawl_domain(domain: str, redis, max_pages: int = None) -> dict:
    """Fetch up to ``max_pages`` pages of ``domain`` and derive the signals rules care about.

    Never raises: a domain that cannot be fetched is a *result* here, and one of the more
    informative ones, so failures come back as ``error`` rather than as an exception.
    """
    if max_pages is None:
        max_pages = settings.MODERATION_PAGES_PER_DOMAIN

    homepage = crawl_url(f"https://{domain}/", redis)
    pages = [_page(homepage)]
    error = (homepage.get("error") or {}).get("name", "") if homepage.get("error") else ""

    for url in _follow_up_urls(domain, homepage, max_pages - 1):
        if CRAWL_DELAY_SECONDS:
            time.sleep(CRAWL_DELAY_SECONDS)
        pages.append(_page(crawl_url(url, redis)))

    return {
        "http_status": homepage.get("status"),
        "final_domain": _final_domain(homepage, domain),
        "error": error,
        "pages": pages,
        "signals": _signals(domain, pages),
    }


def store_evidence(domain: str, crawl: dict) -> DomainEvidence:
    """Persist a crawl and the checks derived from it, ready for the queue to read."""
    evidence, _ = DomainEvidence.objects.update_or_create(
        domain=domain,
        defaults={
            "state": DomainEvidence.State.READY,
            "fetched_at": timezone.now(),
            "http_status": crawl["http_status"],
            "final_domain": crawl["final_domain"],
            "error": crawl["error"],
            "pages": crawl["pages"],
            "signals": crawl["signals"],
            "evidence": [item.to_dict() for item in rules.crawl_evidence(domain, crawl)],
        },
    )
    return evidence


def _page(result: dict) -> dict:
    content = result.get("content") or {}
    error = result.get("error") or {}
    return {
        "url": result.get("url", ""),
        "status": result.get("status"),
        "title": content.get("title", ""),
        "extract": content.get("extract", ""),
        "num_links": len(content.get("links") or []) + len(content.get("extra_links") or []),
        "error": error.get("name", ""),
    }


def _follow_up_urls(domain: str, homepage: dict, limit: int) -> list[str]:
    """Pick further pages from the homepage's links: same site, shallow, not boilerplate.

    Shallow paths are preferred because a site's top-level sections say more about what it
    publishes than an arbitrary deep article does, and they are what a moderator would click.
    """
    if limit <= 0:
        return []
    content = homepage.get("content") or {}
    candidates = list(content.get("links") or []) + list(content.get("extra_links") or [])

    same_site = []
    for url in candidates:
        parsed = urlparse(url)
        if rules.registrable(parsed.netloc) != rules.registrable(domain):
            continue
        path = parsed.path.strip("/")
        if not path or any(word in path.lower() for word in BORING_PATH_WORDS):
            continue
        same_site.append(url)

    same_site.sort(key=lambda url: (urlparse(url).path.count("/"), len(url)))
    return same_site[:limit]


def _final_domain(homepage: dict, domain: str) -> str:
    """The host we ended up on. crawl_url follows redirects and reports the URL it fetched."""
    final = urlparse(homepage.get("url") or "").netloc
    return final if final and final != domain else ""


def _signals(domain: str, pages: list[dict]) -> dict:
    text = " ".join(f"{page['title']} {page['extract']}" for page in pages).lower()
    try:
        blacklisted = get_snapshot_blacklist().is_domain_blacklisted(domain)
    except Exception:
        # The snapshot lives in Redis and is rebuilt periodically; if it is not there yet this
        # is one missing evidence line, not a reason to fail the whole enrichment.
        logger.warning("Blacklist snapshot unavailable while enriching %s", domain)
        blacklisted = False

    return {
        "has_links": any(page["num_links"] for page in pages),
        "num_pages_fetched": sum(1 for page in pages if page["status"] and not page["error"]),
        "ad_markers": sum(marker in text for marker in AD_MARKERS),
        "paywall_markers": sum(marker in text for marker in PAYWALL_MARKERS),
        "blacklisted": blacklisted,
    }


def page_texts(evidence: DomainEvidence | None) -> list[str]:
    """The text the model reads, in the same shape training built it from."""
    if evidence is None:
        return []
    return [f"{page.get('title', '')} {page.get('extract', '')}".strip()
            for page in (evidence.pages or [])]
