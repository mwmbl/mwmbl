"""Libraries.io package search via the API.

Searches across all package managers tracked by libraries.io (PyPI, npm, RubyGems, etc.).
Returns documents with links to repository and documentation URLs.
"""
import logging
import os

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

ENDPOINT = "https://libraries.io/api/search"
API_KEY = os.getenv("LIBRARIES_IO_API_KEY")


def _check_api_key() -> bool:
    """Check if API key is configured. Raises exception if missing."""
    if not API_KEY:
        raise ValueError(
            "LIBRARIES_IO_API_KEY environment variable is required for Super Search. "
            "Please set it in your .env file or environment."
        )
    return True


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    _check_api_key()
    
    try:
        response = await client.get(
            ENDPOINT,
            params={"q": query, "api_key": API_KEY, "per_page": limit},
        )
        if response.status_code == 401:
            logger.error("Libraries.io API authentication failed - check API key")
            return []
        if response.status_code == 429:
            logger.warning("Libraries.io API rate limit exceeded - skipping source")
            return []
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.info("Libraries.io source failed: %s", e)
        return []
    
    docs: list[Document] = []
    # libraries.io API returns results as a list directly
    results = payload if isinstance(payload, list) else payload.get("results", []) or payload.get("packages", []) or []
    for item in results:
        name = item.get("name") or ""
        platform = item.get("platform", "Unknown")
        description = item.get("description") or item.get("readme") or ""
        
        # Include BOTH repository and documentation URLs
        repository_url = item.get("repository_url")
        documentation_url = item.get("documentation_url")
        
        # Create separate Document for each URL
        if repository_url:
            title = f"{name} ({platform})" if platform else name
            docs.append(Document(title=title, url=repository_url, extract=description))
        
        if documentation_url and documentation_url != repository_url:
            title = f"{name} Documentation ({platform})" if platform else f"{name} Documentation"
            docs.append(Document(title=title, url=documentation_url, extract=description))
    
    return docs
