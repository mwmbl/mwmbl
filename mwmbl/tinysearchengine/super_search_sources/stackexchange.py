"""Stack Exchange (Stack Overflow site) search via the public API.

Unauthenticated requests are subject to a 300/day quota per IP. The adapter
silently returns [] on quota or transport errors.
"""
import html
import logging

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.stackexchange.com/2.3/search/advanced"


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    try:
        response = await client.get(
            ENDPOINT,
            params={
                "q": query,
                "site": "stackoverflow",
                "order": "desc",
                "sort": "relevance",
                "pagesize": limit,
            },
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.info("Stack Exchange source failed: %s", e)
        return []

    docs: list[Document] = []
    for item in payload.get("items", []):
        url = item.get("link")
        if not url:
            continue
        title = html.unescape(item.get("title") or "")
        tags = " ".join(item.get("tags", []))
        score = item.get("score")
        bits = []
        if tags:
            bits.append(f"Tags: {tags}.")
        if score is not None:
            bits.append(f"Score: {score}.")
        extract = " ".join(bits)
        docs.append(Document(title=title, url=url, extract=extract))
    return docs
