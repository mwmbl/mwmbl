"""Proof-of-concept image search, proxied to the Brave Image Search API.

Results are cached in the shared cache (Redis in production) for an hour, keyed like the
external results cache: a keyed hash of the normalised query, so the cache never holds the
query text. Brave's terms only allow "transient storage required for operation", which is
why this is a short-lived cache and not a store like the Staan one. One entry answers every
`count`, because the fetch always asks Brave for MAX_COUNT and the request is sliced.

Only one request per query goes to Brave at a time. The first takes a short lock; others
asking for the same query meanwhile wait for its answer to land in the cache, so a burst of
identical searches costs one call, not one each.
"""

from logging import getLogger
from time import monotonic, sleep

import httpx
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django_redis.exceptions import ConnectionInterrupted
from ninja import Query, Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from mwmbl import provider_usage
from mwmbl.indexer.external_cache import external_cache_term

logger = getLogger(__name__)

BRAVE_IMAGE_SEARCH_URL = "https://api.search.brave.com/res/v1/images/search"

MAX_COUNT = 100

CACHE_KEY = "image-search:{term}"

BRAVE_TIMEOUT_SECONDS = 10
LOCK_KEY = "image-search-lock:{term}"
# Outlives the slowest fetch, so a worker that dies mid-fetch frees the query in time.
LOCK_SECONDS = BRAVE_TIMEOUT_SECONDS + 5
# How long a request waits on another's fetch before fetching for itself.
LOCK_WAIT_SECONDS = BRAVE_TIMEOUT_SECONDS + 1
LOCK_POLL_SECONDS = 0.1

# A 429 pauses every Brave call, across workers, for as long as Brave asks.
CIRCUIT_KEY = "image-search-circuit-open"
CIRCUIT_DEFAULT_SECONDS = 10
CIRCUIT_MAX_SECONDS = 300

# Lets the browser reuse a page of results on back and forward without asking us again.
BROWSER_MAX_AGE_SECONDS = 300

router = Router(tags=["Image Search"])


class ImageSearchParams(Schema):
    q: str = Field(min_length=1, max_length=400)
    count: int = Field(default=40, ge=1, le=MAX_COUNT)


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


def _cache_get(key: str):
    # A cache that is down is a cache miss, never a failed search.
    try:
        return cache.get(key)
    except ConnectionInterrupted:
        logger.warning("Could not read the image search cache", exc_info=True)
        return None


def _cache_set(key: str, value, timeout: int) -> None:
    try:
        cache.set(key, value, timeout=timeout)
    except ConnectionInterrupted:
        logger.warning("Could not write the image search cache", exc_info=True)


def _take_lock(key: str) -> bool:
    # Without a cache to coordinate through, every request fetches for itself.
    try:
        return cache.add(key, 1, timeout=LOCK_SECONDS)
    except ConnectionInterrupted:
        logger.warning("Could not take the image search lock", exc_info=True)
        return True


def _release_lock(key: str) -> None:
    try:
        cache.delete(key)
    except ConnectionInterrupted:
        logger.warning("Could not release the image search lock", exc_info=True)


def _cooldown_seconds(response: httpx.Response) -> int:
    retry_after = response.headers.get("Retry-After", "")
    seconds = int(retry_after) if retry_after.isdigit() else CIRCUIT_DEFAULT_SECONDS
    return min(max(seconds, 1), CIRCUIT_MAX_SECONDS)


def _fetch_from_brave(query: str, api_key: str) -> list[dict]:
    provider_usage.record_provider_call(provider_usage.PROVIDER_BRAVE_IMAGES)
    try:
        response = httpx.get(
            BRAVE_IMAGE_SEARCH_URL,
            params={"q": query, "count": MAX_COUNT, "safesearch": "strict"},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=BRAVE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        provider_usage.record_provider_failure(provider_usage.PROVIDER_BRAVE_IMAGES)
        raise HttpError(502, "Image search is unreachable") from e
    if response.status_code != 200:
        provider_usage.record_provider_failure(provider_usage.PROVIDER_BRAVE_IMAGES)
    if response.status_code == 429:
        _cache_set(CIRCUIT_KEY, 1, _cooldown_seconds(response))
        raise HttpError(429, "Image search is not available right now, try again after a few seconds.")
    if response.status_code != 200:
        raise HttpError(502, f"Brave image search failed with status {response.status_code}")

    results = []
    for item in response.json()["results"]:
        properties = item["properties"]
        image = ImageResult(
            title=item["title"],
            page_url=item["url"],
            source=item["source"],
            thumbnail_url=item["thumbnail"]["src"],
            image_url=properties["url"],
            width=properties.get("width"),
            height=properties.get("height"),
        )
        results.append(image.model_dump())
    return results


def _cached_or_fetched(query: str, api_key: str) -> list[dict]:
    term = external_cache_term(query)
    cache_key = CACHE_KEY.format(term=term)
    lock_key = LOCK_KEY.format(term=term)
    deadline = monotonic() + LOCK_WAIT_SECONDS
    while True:
        results = _cache_get(cache_key)
        if results is not None:
            provider_usage.record_provider_cache_hit(provider_usage.PROVIDER_BRAVE_IMAGES)
            return results
        if _cache_get(CIRCUIT_KEY) is not None:
            raise HttpError(429, "Image search is not available right now, try again after a few seconds.")
        holds_lock = _take_lock(lock_key)
        if holds_lock or monotonic() >= deadline:
            break
        sleep(LOCK_POLL_SECONDS)

    # Stored before the lock is released, or a waiter can find neither and fetch again.
    try:
        # Checked again under the lock: the last holder may have stored it since we looked.
        results = _cache_get(cache_key) if holds_lock else None
        if results is not None:
            provider_usage.record_provider_cache_hit(provider_usage.PROVIDER_BRAVE_IMAGES)
            return results
        results = _fetch_from_brave(query, api_key)
        # Brave's terms (Search API Terms of Use, 3(b)(i)) forbid caching results beyond
        # "transient storage required for operation", hence the short TTLs in settings.
        ttl = settings.BRAVE_IMAGE_CACHE_TTL_SECONDS if results else settings.BRAVE_IMAGE_CACHE_EMPTY_TTL_SECONDS
        _cache_set(cache_key, results, ttl)
    finally:
        if holds_lock:
            _release_lock(lock_key)
    return results


@router.get("/search", response=ImageSearchResponse, auth=None, summary="Search for images")
def search_images(request, response: HttpResponse, params: Query[ImageSearchParams]):
    api_key = settings.BRAVE_SEARCH_API_KEY
    if not api_key:
        raise HttpError(503, "BRAVE_SEARCH_API_KEY is not configured")

    results = _cached_or_fetched(params.q, api_key)
    response["Cache-Control"] = f"private, max-age={BROWSER_MAX_AGE_SECONDS}"
    return {"results": results[: params.count]}
