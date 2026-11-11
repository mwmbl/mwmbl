"""Mwmbl index + Wikipedia via the standard search ranker."""

import logging

import httpx
from asgiref.sync import sync_to_async

from mwmbl.search_setup import ranker
from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    try:
        # Not asyncio.to_thread: the ranker looks Commons files up in the store (mwmbl.imagesearch.serving), and only
        # the request's own thread has its database connection closed when the request ends.
        results = await sync_to_async(ranker.search)(query, [])
        return results[:limit]
    except Exception as e:
        logger.info("mwmbl index source failed: %s", e)
        return []
