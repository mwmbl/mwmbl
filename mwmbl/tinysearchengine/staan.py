from logging import getLogger

import requests
from django.conf import settings

logger = getLogger(__name__)

STAAN_SEARCH_URL = "https://api.staan.ai/v2/search/web"


def get_staan_results(query: str, market: str = "en-us") -> list[dict]:
    if not settings.STAAN_SEARCH_API_KEY:
        logger.warning("STAAN_SEARCH_API_KEY is not configured")
        return []

    try:
        response = requests.get(
            STAAN_SEARCH_URL,
            params={"q": query, "market": market},
            headers={"Authorization": f"Bearer {settings.STAAN_SEARCH_API_KEY}"},
            timeout=5,
        )
        response.raise_for_status()
        raw_results = response.json()["web"]["results"]
    except Exception:
        logger.exception("Failed to fetch Staan results for query %s", query)
        return []

    return [
        {
            "url": r["url"],
            "title": [{"value": r.get("title", ""), "is_bold": False}],
            "extract": [{"value": r.get("snippet") or r.get("description", ""), "is_bold": False}],
            "source": "staan",
        }
        for r in raw_results
        if r.get("url")
    ]
