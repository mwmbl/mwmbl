"""IMDb title/name lookup via the public autosuggest API.

IMDb has no open full-text search API, but the autosuggest endpoint that powers
the site's search box returns titles and people ranked by relevance, each with a
stable id (``tt…`` for titles, ``nm…`` for names) that maps to the canonical
``/title/`` or ``/name/`` page. We emit the ``www.imdb.com`` form with a trailing
slash, which is how the gold dataset records the majority of IMDb URLs.

The query goes in the URL path (not a query-string param), so this can't be a
declarative recipe; the path is bucketed by the query's first alphanumeric char,
as the endpoint expects.
"""
import logging
from urllib.parse import quote

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://v3.sg.media-imdb.com/suggestion/{bucket}/{query}.json"


def _url_for(item_id: str) -> str:
    if item_id.startswith("tt"):
        return f"https://www.imdb.com/title/{item_id}/"
    if item_id.startswith("nm"):
        return f"https://www.imdb.com/name/{item_id}/"
    return ""


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    q = query.strip()
    if not q:
        return []
    bucket = next((c for c in q.lower() if c.isalnum()), "x")
    url = SUGGEST_URL.format(bucket=bucket, query=quote(q, safe=""))
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.info("IMDb source failed: %s", e)
        return []

    docs: list[Document] = []
    for item in payload.get("d", [])[:limit]:
        target = _url_for(item.get("id", ""))
        if not target:
            continue
        # 'l' is the title/name; 's' the cast/role blurb; 'q' the kind (feature, TV series…).
        extract = item.get("s") or item.get("q") or ""
        docs.append(Document(title=item.get("l", ""), url=target, extract=extract))
    return docs
