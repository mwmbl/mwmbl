"""Mwmbl index + Wikipedia via the standard search ranker."""
import asyncio
import logging

import httpx

from mwmbl.search_setup import ranker
from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    try:
        results = await asyncio.to_thread(ranker.search, query, [])
        return results[:limit]
    except Exception as e:
        logger.info("mwmbl index source failed: %s", e)
        return []
