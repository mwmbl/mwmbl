"""Hacker News via the Algolia search API (no auth required)."""
import logging

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

ENDPOINT = "https://hn.algolia.com/api/v1/search"


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    try:
        response = await client.get(
            ENDPOINT,
            params={"query": query, "hitsPerPage": limit, "tags": "story"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.info("HN source failed: %s", e)
        return []

    docs: list[Document] = []
    for hit in payload.get("hits", []):
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        title = hit.get("title") or hit.get("story_title") or ""
        extract = hit.get("story_text") or hit.get("comment_text") or ""
        if not title and not extract:
            continue
        docs.append(Document(title=title, url=url, extract=extract))
    return docs
