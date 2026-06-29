"""The Guardian — UK/world news via the Content API.

Needs a (free) API key in ``settings.GUARDIAN_API_KEY`` (env ``GUARDIAN_API_KEY``).
Without a key the source is a no-op (returns []), so it degrades gracefully in
environments where the key isn't configured. Covers the "news" intent that the
tech/academic-skewed catalog otherwise misses.
"""
import logging

import httpx
from django.conf import settings

from mwmbl.crawler.retrieve import extract_from_html_text
from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

ENDPOINT = "https://content.guardianapis.com/search"


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    api_key = getattr(settings, "GUARDIAN_API_KEY", "")
    if not api_key:
        return []
    try:
        response = await client.get(ENDPOINT, params={
            "q": query,
            "api-key": api_key,
            "show-fields": "trailText",
            "order-by": "relevance",
            "page-size": max(1, min(limit, 10)),
        })
        response.raise_for_status()
        results = response.json().get("response", {}).get("results", [])
    except (httpx.HTTPError, ValueError) as e:
        logger.info("Guardian source failed: %s", e)
        return []

    docs: list[Document] = []
    for item in results:
        title = item.get("webTitle") or ""
        url = item.get("webUrl") or ""
        trail = (item.get("fields") or {}).get("trailText") or ""
        extract = extract_from_html_text(trail) if trail else ""
        if title and url:
            docs.append(Document(title=title, url=url, extract=extract))
    return docs
