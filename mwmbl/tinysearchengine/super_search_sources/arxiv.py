"""ArXiv search via the public Atom API."""
import logging
from xml.etree import ElementTree as ET

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

ENDPOINT = "https://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
}


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    try:
        response = await client.get(
            ENDPOINT,
            params={
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": limit,
            },
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
    except (httpx.HTTPError, ET.ParseError) as e:
        logger.info("ArXiv source failed: %s", e)
        return []

    docs: list[Document] = []
    for entry in root.findall("atom:entry", NS):
        url_el = entry.find("atom:id", NS)
        title_el = entry.find("atom:title", NS)
        summary_el = entry.find("atom:summary", NS)
        if url_el is None or url_el.text is None:
            continue
        url = url_el.text.strip()
        title = (title_el.text or "").strip() if title_el is not None else ""
        extract = (summary_el.text or "").strip() if summary_el is not None else ""
        docs.append(Document(title=title, url=url, extract=extract))
    return docs
