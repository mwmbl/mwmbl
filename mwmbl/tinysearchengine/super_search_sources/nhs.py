"""NHS website search (nhs.uk) — authoritative UK consumer-health information.

Covers the health/medical intent (conditions, medicines, symptoms) that the
tech/academic-skewed source catalogue otherwise misses. The NHS search page is
server-rendered; each result is an ``a.app-search-results-item`` whose real
target path is URL-encoded in the click-tracker's ``url`` query parameter.
"""
import logging
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.nhs.uk/search/results"
BASE = "https://www.nhs.uk"
_UA = "Mozilla/5.0 (compatible; mwmbl-supersearch/1.0; +https://mwmbl.org)"


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    try:
        response = await client.get(ENDPOINT, params={"q": query}, headers={"User-Agent": _UA})
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.info("NHS source failed: %s", e)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    docs: list[Document] = []
    for a in soup.select("a.app-search-results-item")[:limit]:
        href = a.get("href") or ""
        title = a.get_text(" ", strip=True)
        # The clean page path is the `url` param of the click-tracking href.
        target = ""
        params = parse_qs(urlsplit(href).query)
        if params.get("url"):
            target = unquote(params["url"][0])
        url = urljoin(BASE, target or href)
        if title and url:
            docs.append(Document(title=title, url=url, extract=""))
    return docs
