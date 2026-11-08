"""Live image search over the Wikimedia Commons Action API.

The query uses the search profile and filters Commons' own MediaSearch uses, and falls back to
plain file search if Commons rejects them. One request returns the candidates together with
everything the safety checks read: categories (hidden ones included), description and licence.

Wikimedia rate-limits API clients globally and asks them to identify themselves and to honour
429 and 503 with Retry-After. A back-off is kept in the cache, so one overloaded response pauses
image search for every worker rather than being rediscovered by each.
"""

import html
import re
import threading
from urllib.parse import urlparse

import httpx
from django.conf import settings
from django.core.cache import cache

from mwmbl.imagesearch.types import ImageSourceUnavailable, SourceImage

COOLDOWN_CACHE_KEY = "imagesearch:commons-cooldown"
DEFAULT_COOLDOWN_SECONDS = 60
ALLOWED_THUMBNAIL_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
THUMBNAIL_WIDTH = 320
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")

request_slots = threading.BoundedSemaphore(settings.COMMONS_MAX_CONCURRENT_REQUESTS)


def new_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": settings.IMAGE_SEARCH_USER_AGENT},
        timeout=settings.COMMONS_TIMEOUT_SECONDS,
        follow_redirects=False,
    )


def get_with_backoff(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    """GET from Wikimedia, honouring and recording any request to back off."""
    if cache.get(COOLDOWN_CACHE_KEY) is not None:
        raise ImageSourceUnavailable("Wikimedia asked us to back off")

    try:
        with request_slots:
            response = client.get(url, params=params)
    except httpx.HTTPError as e:
        raise ImageSourceUnavailable(f"Wikimedia request failed: {e!r}") from e

    if response.status_code in (429, 503):
        retry_after = response.headers.get("Retry-After", "")
        seconds = int(retry_after) if retry_after.isdigit() else DEFAULT_COOLDOWN_SECONDS
        cache.set(COOLDOWN_CACHE_KEY, True, min(seconds, settings.COMMONS_MAX_COOLDOWN_SECONDS))
        raise ImageSourceUnavailable(f"Wikimedia returned {response.status_code}")
    response.raise_for_status()
    return response


def plain_text(markup: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", html.unescape(TAG_PATTERN.sub(" ", markup))).strip()


def metadata_value(extmetadata: dict, key: str) -> str:
    entry = extmetadata.get(key)
    return plain_text(str(entry["value"])) if entry else ""


def parse_page(page: dict) -> SourceImage | None:
    """Turn a Commons page into a SourceImage, or None when the metadata a safety check needs is missing."""
    image_info = page.get("imageinfo")
    if not image_info:
        return None
    info = image_info[0]

    thumbnail_url = info.get("thumburl", "")
    parsed = urlparse(thumbnail_url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_THUMBNAIL_HOSTS:
        return None
    if info.get("mime") not in ALLOWED_MIME_TYPES:
        return None

    extmetadata = info.get("extmetadata", {})
    categories = tuple(c for c in metadata_value(extmetadata, "Categories").split("|") if c)
    if not categories:
        return None

    title = page["title"].removeprefix("File:")
    description = metadata_value(extmetadata, "ImageDescription")
    return SourceImage(
        title=title,
        page_url=info["descriptionurl"],
        thumbnail_url=thumbnail_url,
        thumbnail_width=info["thumbwidth"],
        thumbnail_height=info["thumbheight"],
        description=description,
        categories=categories,
        license_name=metadata_value(extmetadata, "LicenseShortName"),
        license_url=metadata_value(extmetadata, "LicenseUrl"),
        attribution=metadata_value(extmetadata, "Artist"),
        rank=page["index"],
    )


def request_params(search_expression: str, limit: int, mediasearch: bool) -> dict:
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "search",
        "gsrnamespace": 6,
        "gsrsearch": search_expression,
        "gsrlimit": limit,
        "gsrsort": "relevance",
        "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata",
        "iiurlwidth": THUMBNAIL_WIDTH,
        "iiextmetadatafilter": "Categories|ImageDescription|LicenseShortName|LicenseUrl|Artist",
    }
    if mediasearch:
        params["gsrqiprofile"] = "classic_noboostlinks"
    return params


def search_commons(client: httpx.Client, query: str, limit: int) -> list[SourceImage]:
    """Candidate images for a query, best first. Raises ImageSourceUnavailable on any failure."""
    expression = f"filetype:bitmap -fileres:0 {query}"
    try:
        response = get_with_backoff(client, settings.COMMONS_API_URL, request_params(expression, limit, True))
    except httpx.HTTPStatusError:
        response = get_with_backoff(client, settings.COMMONS_API_URL, request_params(query, limit, False))

    body = response.json()
    if "error" in body:
        raise ImageSourceUnavailable(f"Commons API error: {body['error'].get('code')}")

    pages = body.get("query", {}).get("pages", [])
    images = [image for image in map(parse_page, pages) if image is not None]
    return sorted(images, key=lambda image: image.rank)
