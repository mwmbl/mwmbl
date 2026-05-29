"""PyPI lookup via the JSON API.

PyPI has no public full-text search endpoint, so we treat the query as a
package name and look it up directly. Returns at most one Document.
"""
import logging
import re

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

ENDPOINT = "https://pypi.org/pypi/{name}/json"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    name = query.strip().split()[0] if query.strip() else ""
    if not name or not NAME_RE.match(name):
        return []

    try:
        response = await client.get(ENDPOINT.format(name=name))
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.info("PyPI source failed: %s", e)
        return []

    info = payload.get("info") or {}
    url = info.get("package_url") or f"https://pypi.org/project/{name}/"
    title = info.get("name") or name
    extract = info.get("summary") or ""
    return [Document(title=title, url=url, extract=extract)]
