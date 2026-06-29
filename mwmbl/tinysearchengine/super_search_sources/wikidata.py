"""Official-website resolver via Wikidata.

Resolves the query to Wikidata entities (``wbsearchentities``) and returns each
entity's official website (property ``P856``). This is the structured, Big-Tech-
independent way to answer navigational/brand queries ("virgin media" ->
https://www.virginmedia.com/) without a per-brand source.
"""
import logging

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

API = "https://www.wikidata.org/w/api.php"
OFFICIAL_WEBSITE = "P856"
# Wikimedia's User-Agent policy blocks generic/library UAs; identify ourselves.
_HEADERS = {"User-Agent": "mwmbl-supersearch/1.0 (https://mwmbl.org; hello@mwmbl.org)"}


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    n = max(1, min(limit, 5))
    try:
        r = await client.get(API, headers=_HEADERS, params={
            "action": "wbsearchentities", "search": query,
            "language": "en", "format": "json", "limit": n,
        })
        r.raise_for_status()
        hits = r.json().get("search", [])
    except (httpx.HTTPError, ValueError) as e:
        logger.info("Wikidata search failed: %s", e)
        return []

    ids = [h["id"] for h in hits[:n] if h.get("id")]
    if not ids:
        return []

    try:
        r2 = await client.get(API, headers=_HEADERS, params={
            "action": "wbgetentities", "ids": "|".join(ids),
            "props": "claims|labels|descriptions", "languages": "en", "format": "json",
        })
        r2.raise_for_status()
        entities = r2.json().get("entities", {})
    except (httpx.HTTPError, ValueError) as e:
        logger.info("Wikidata entities failed: %s", e)
        return []

    docs: list[Document] = []
    for qid in ids:  # preserve search-rank order
        entity = entities.get(qid) or {}
        label = (entity.get("labels", {}).get("en") or {}).get("value", "")
        desc = (entity.get("descriptions", {}).get("en") or {}).get("value", "")
        for claim in entity.get("claims", {}).get(OFFICIAL_WEBSITE, []):
            try:
                url = claim["mainsnak"]["datavalue"]["value"]
            except (KeyError, TypeError):
                continue
            if url:
                docs.append(Document(title=label or url, url=url, extract=desc))
                break  # one official site per entity
    return docs
