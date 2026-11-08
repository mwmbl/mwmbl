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


class SourceRejectedRequest(ImageSourceUnavailable):
    """Wikimedia understood the request and refused it (a 4xx other than a rate limit, or an API error)."""


COOLDOWN_CACHE_KEY = "imagesearch:commons-cooldown"
DEFAULT_COOLDOWN_SECONDS = 60
ALLOWED_THUMBNAIL_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}
COMMONS_PAGE_HOSTS = {"commons.wikimedia.org"}
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
THUMBNAIL_WIDTH = 320
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")
# Characters that turn a query into CirrusSearch syntax (incategory:, insource:, phrases,
# wildcards, negation...). They would let a caller steer which files are candidates.
SEARCH_SYNTAX_PATTERN = re.compile(r"[:\"~*?\\()\[\]{}|!^<>]")
LEADING_HYPHENS_PATTERN = re.compile(r"(?<!\S)-+")

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
    if response.is_error:
        raise SourceRejectedRequest(f"Wikimedia returned {response.status_code}")
    return response


def plain_text(markup: str) -> str:
    """Markup as text. Entities are decoded first so that encoded tags are stripped too, not revived."""
    return WHITESPACE_PATTERN.sub(" ", TAG_PATTERN.sub(" ", html.unescape(markup))).strip()


def https_url_on(url: str, hosts: set[str]) -> str:
    parsed = urlparse(url)
    return url if parsed.scheme == "https" and parsed.hostname in hosts else ""


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
    if not https_url_on(thumbnail_url, ALLOWED_THUMBNAIL_HOSTS):
        return None
    page_url = https_url_on(info.get("descriptionurl", ""), COMMONS_PAGE_HOSTS)
    if not page_url:
        return None
    if info.get("mime") not in ALLOWED_MIME_TYPES:
        return None

    extmetadata = info.get("extmetadata", {})
    categories = tuple(c for c in metadata_value(extmetadata, "Categories").split("|") if c)
    if not categories:
        return None

    title = page["title"].removeprefix("File:")
    description = metadata_value(extmetadata, "ImageDescription")
    license_url = metadata_value(extmetadata, "LicenseUrl")
    return SourceImage(
        title=title,
        page_url=page_url,
        thumbnail_url=thumbnail_url,
        thumbnail_width=info["thumbwidth"],
        thumbnail_height=info["thumbheight"],
        description=description,
        categories=categories,
        license_name=metadata_value(extmetadata, "LicenseShortName"),
        # The licence page is written by the uploader, so only a plain https link is passed on.
        license_url=license_url if urlparse(license_url).scheme == "https" else "",
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
    spaced = WHITESPACE_PATTERN.sub(" ", SEARCH_SYNTAX_PATTERN.sub(" ", query)).strip()
    terms = LEADING_HYPHENS_PATTERN.sub("", spaced)
    if not terms:
        return []
    mediasearch_params = request_params(f"filetype:bitmap -fileres:0 {terms}", limit, True)
    try:
        body = fetch_json(client, mediasearch_params)
    except SourceRejectedRequest:
        body = fetch_json(client, request_params(terms, limit, False))

    pages = body.get("query", {}).get("pages", [])
    images = [image for image in map(parse_page, pages) if image is not None]
    return sorted(images, key=lambda image: image.rank)


def fetch_json(client: httpx.Client, params: dict) -> dict:
    response = get_with_backoff(client, settings.COMMONS_API_URL, params)
    try:
        body = response.json()
    except ValueError as e:
        raise ImageSourceUnavailable("Commons returned a body that is not JSON") from e
    if "error" in body:
        raise SourceRejectedRequest(f"Commons API error: {body['error'].get('code')}")
    return body
