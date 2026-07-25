"""You.com web search adapter for mwmbl Super Search.

Provides web search results from You.com's search API with optional authentication.
Falls back to keyless tier (100 free searches/day per IP) when no API key is configured.
"""
import logging
import os

import httpx

from mwmbl.tinysearchengine.indexer import Document

logger = logging.getLogger(__name__)

# You.com Search API endpoints
SEARCH_ENDPOINT = "https://api.you.com/v1/agents/search"
KEYLESS_ENDPOINT = "https://api.you.com/v1/agents/search"


async def search(client: httpx.AsyncClient, query: str, limit: int) -> list[Document]:
    """Search You.com and return formatted results.
    
    Args:
        client: HTTP client for making requests
        query: Search query string
        limit: Maximum number of results to return
        
    Returns:
        List of Document objects with title, URL, and extract
    """
    try:
        # Check for API key
        api_key = os.getenv("YDC_API_KEY")
        
        # Prepare headers and endpoint
        headers = {"User-Agent": "youdotcom-integration/mwmbl-mwmbl"}
        endpoint = SEARCH_ENDPOINT
        
        if api_key:
            headers["X-API-Key"] = api_key
        else:
            # Use keyless endpoint for unauthenticated requests
            endpoint = KEYLESS_ENDPOINT
            
        # Make request to You.com Search API
        response = await client.get(
            endpoint,
            params={"query": query, "count": min(limit, 20)},  # You.com max is 20
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.info("You.com source failed: %s", e)
        return []
    
    # Parse results from You.com response
    docs: list[Document] = []
    
    # Extract web results
    web_results = payload.get("results", {}).get("web", [])
    for item in web_results:
        url = item.get("url")
        if not url:
            continue
            
        title = item.get("title", "")
        description = item.get("description", "")
        
        # Use description as extract, fallback to title
        extract = description if description else title
        
        docs.append(Document(
            title=title,
            url=url,
            extract=extract
        ))
        
        # Respect the limit
        if len(docs) >= limit:
            break
    
    logger.debug(f"You.com source returned {len(docs)} results for query: {query}")
    return docs