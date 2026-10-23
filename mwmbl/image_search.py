"""Proof-of-concept image search, proxied to the Brave Image Search API."""

import httpx
from django.conf import settings
from ninja import Query, Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from mwmbl import provider_usage

BRAVE_IMAGE_SEARCH_URL = "https://api.search.brave.com/res/v1/images/search"

router = Router(tags=["Image Search"])


class ImageSearchParams(Schema):
    q: str = Field(min_length=1, max_length=400)
    count: int = Field(default=40, ge=1, le=100)


class ImageResult(Schema):
    title: str
    page_url: str
    source: str
    thumbnail_url: str
    image_url: str
    width: int | None
    height: int | None


class ImageSearchResponse(Schema):
    results: list[ImageResult]


@router.get("/search", response=ImageSearchResponse, auth=None, summary="Search for images")
def search_images(request, params: Query[ImageSearchParams]):
    api_key = settings.BRAVE_SEARCH_API_KEY
    if not api_key:
        raise HttpError(503, "BRAVE_SEARCH_API_KEY is not configured")

    provider_usage.record_provider_call(provider_usage.PROVIDER_BRAVE_IMAGES)
    try:
        response = httpx.get(
            BRAVE_IMAGE_SEARCH_URL,
            params={"q": params.q, "count": params.count, "safesearch": "strict"},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=10,
        )
    except httpx.HTTPError as e:
        provider_usage.record_provider_failure(provider_usage.PROVIDER_BRAVE_IMAGES)
        raise HttpError(502, "Image search is unreachable") from e
    if response.status_code != 200:
        provider_usage.record_provider_failure(provider_usage.PROVIDER_BRAVE_IMAGES)
    if response.status_code == 429:
        raise HttpError(429, "Image search is not available right now, try again after a few seconds.")
    if response.status_code != 200:
        raise HttpError(502, f"Brave image search failed with status {response.status_code}")

    results = []
    for item in response.json()["results"]:
        properties = item["properties"]
        results.append(
            ImageResult(
                title=item["title"],
                page_url=item["url"],
                source=item["source"],
                thumbnail_url=item["thumbnail"]["src"],
                image_url=properties["url"],
                width=properties.get("width"),
                height=properties.get("height"),
            )
        )
    return {"results": results}
