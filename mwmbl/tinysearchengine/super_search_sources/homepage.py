"""Heuristic brand-homepage guesser for navigational queries.

Many navigational queries are a brand/site name with no dedicated source and no
Wikidata entity ("whitakers keighley", "solvera jewellery"). This slugifies the
query, tries a few common TLDs, and keeps a homepage only if the fetched page's
title actually contains a query term — a cheap relevance guard against random
domain squatters. It complements (does not replace) the general index: it just
surfaces an official homepage candidate the crawl may not have indexed yet.
"""
import logging
import re

import httpx
from bs4 import BeautifulSoup

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

TLDS = (".com", ".co.uk", ".org", ".net")
_UA = "Mozilla/5.0 (compatible; mwmbl-supersearch/1.0; +https://mwmbl.org)"


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    slug = re.sub(r"[^a-z0-9]", "", query.lower())
    terms = [t for t in re.split(r"\s+", query.lower().strip()) if len(t) > 1]
    if len(slug) < 3 or not terms:
        return []

    docs: list[Document] = []
    seen: set[str] = set()
    for tld in TLDS:
        if len(docs) >= limit:
            break
        candidate = f"https://{slug}{tld}/"
        try:
            r = await client.get(candidate, follow_redirects=True,
                                  headers={"User-Agent": _UA})
        except httpx.HTTPError:
            continue
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        final = str(r.url)
        if final in seen:
            continue
        seen.add(final)
        title = ""
        if (soup := BeautifulSoup(r.text, "html.parser")).title and soup.title.string:
            title = soup.title.string.strip()
        # Keep only if a query term appears in the title (cheap relevance guard).
        if title and any(t in title.lower() for t in terms):
            docs.append(Document(title=title, url=final, extract=""))
    return docs
