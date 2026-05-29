"""GitHub repository search via the unauthenticated REST API.

Unauthenticated calls are limited to 10 requests/minute for the search endpoint,
which is acceptable for the initial Super Search release. Add a token later
to lift the cap.
"""
import logging

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.github.com/search/repositories"


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    try:
        response = await client.get(
            ENDPOINT,
            params={"q": query, "per_page": limit},
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.info("GitHub source failed: %s", e)
        return []

    docs: list[Document] = []
    for item in payload.get("items", []):
        url = item.get("html_url")
        if not url:
            continue
        title = item.get("full_name") or item.get("name") or ""
        extract = item.get("description") or ""
        docs.append(Document(title=title, url=url, extract=extract))
    return docs
