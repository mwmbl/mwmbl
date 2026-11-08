"""The image search pipeline: Commons candidates in, approved images out.

Every layer can only remove images, and a layer that cannot decide removes them too, so an
image is shown only if each of them says SAFE:

1. the query itself is not sexual (rules, then the text classifier);
2. the image's text passes the deterministic rules;
3. the text classifier calls the image's text safe;
4. NudeNet finds no exposed body parts in a thumbnail that is fetched, scored and discarded.

Nothing here can make a response a guarantee: no layer detects CSAM, the models read English
only, and a Commons file can be replaced after it is approved, since thumbnail URLs are not
tied to a revision. The layers lower the odds; they do not remove them.

Nothing is persisted except the approved results, which sit in the cache briefly. The browser
loads approved thumbnails straight from Wikimedia.
"""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import httpx
from django.conf import settings
from django.core.cache import cache

from mwmbl.imagesearch import rules
from mwmbl.imagesearch.commons import new_client, search_commons
from mwmbl.imagesearch.pixel_classifier import PixelSafetyClassifier, fetch_thumbnail
from mwmbl.imagesearch.text_classifier import TextSafetyClassifier
from mwmbl.imagesearch.types import ApprovedImage, SourceImage, Verdict

SOURCE_NAME = "wikimedia-commons"
CACHE_KEY_PREFIX = "imagesearch:v1:"
THUMBNAIL_FETCH_WORKERS = settings.COMMONS_MAX_CONCURRENT_REQUESTS
STOPWORDS = {"a", "an", "and", "the", "of", "in", "on", "at", "for", "to", "with", "photo", "picture", "image"}
WORD_PATTERN = re.compile(r"[a-z0-9]+")
MAX_STRONG_MATCH_TERMS = 4


@lru_cache(maxsize=1)
def text_classifier() -> TextSafetyClassifier:
    return TextSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)


@lru_cache(maxsize=1)
def pixel_classifier() -> PixelSafetyClassifier:
    return PixelSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)


def normalise_query(query: str) -> str:
    return " ".join(query.lower().split())


def query_is_safe(query: str) -> bool:
    """Whether to search at all. Only a confident NSFW verdict refuses the query.

    The text model is noisy on a word or two ("cat" scores uncertain), and an uncertain query
    must not blank out innocent searches. It costs nothing in safety: every image still has to pass
    all of its own layers.
    """
    if rules.query_verdict(query) is not Verdict.SAFE:
        return False
    (verdict,) = text_classifier().classify([query])
    return verdict is not Verdict.NSFW


def pass_text_checks(images: list[SourceImage]) -> list[SourceImage]:
    ruled = [image for image in images if rules.image_verdict(image) is Verdict.SAFE]
    verdicts = text_classifier().classify([image.text for image in ruled])
    return [image for image, verdict in zip(ruled, verdicts) if verdict is Verdict.SAFE]


def pass_pixel_check(client: httpx.Client, image: SourceImage) -> bool:
    data = fetch_thumbnail(client, image.thumbnail_url)
    if data is None:
        return False
    return pixel_classifier().classify_bytes(data) is Verdict.SAFE


def pass_pixel_checks(client: httpx.Client, images: list[SourceImage]) -> list[SourceImage]:
    with ThreadPoolExecutor(max_workers=THUMBNAIL_FETCH_WORKERS) as pool:
        passed = list(pool.map(lambda image: pass_pixel_check(client, image), images))
    return [image for image, ok in zip(images, passed) if ok]


def approve(image: SourceImage) -> ApprovedImage:
    return ApprovedImage(
        title=image.title,
        page_url=image.page_url,
        thumbnail_url=image.thumbnail_url,
        thumbnail_width=image.thumbnail_width,
        thumbnail_height=image.thumbnail_height,
        source=SOURCE_NAME,
        license_name=image.license_name,
        license_url=image.license_url,
        attribution=image.attribution,
    )


def cache_key(kind: str, query: str) -> str:
    digest = hashlib.sha256(normalise_query(query).encode()).hexdigest()
    return f"{CACHE_KEY_PREFIX}{kind}:{digest}"


def safe_images(client: httpx.Client, candidates: list[SourceImage]) -> list[SourceImage]:
    return pass_pixel_checks(client, pass_text_checks(candidates))


def search_images(query: str, limit: int) -> list[ApprovedImage]:
    """Approved images for the query, best first. Raises ImageSourceUnavailable if Commons cannot be used."""
    key = cache_key("search", query)
    cached = cache.get(key)
    if cached is not None:
        return cached[:limit]

    approved: list[ApprovedImage] = []
    if query_is_safe(query):
        with new_client() as client:
            candidates = search_commons(client, query, settings.COMMONS_CANDIDATES)
            approved = [approve(image) for image in safe_images(client, candidates)]

    cache.set(key, approved[: settings.IMAGE_SEARCH_MAX_RESULTS], settings.IMAGE_SEARCH_CACHE_SECONDS)
    return approved[:limit]


def query_terms(query: str) -> list[str]:
    words = WORD_PATTERN.findall(query.lower())
    return [word for word in words if word not in STOPWORDS]


def singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def is_strong_match(query: str, image: SourceImage) -> bool:
    """Whether the image's title contains every term in a short, specific query, near the top of Commons' ranking."""
    terms = query_terms(query)
    if not 1 <= len(terms) <= MAX_STRONG_MATCH_TERMS:
        return False
    if image.rank > settings.IMAGE_SEARCH_TOP_IMAGE_MAX_RANK:
        return False
    title_terms = {singular(word) for word in WORD_PATTERN.findall(image.title.lower())}
    return all(singular(term) in title_terms for term in terms)


def top_image(query: str) -> ApprovedImage | None:
    """One approved image to show with web results, only when it is a strong match for the query."""
    key = cache_key("top", query)
    cached = cache.get(key)
    if cached is not None:
        return cached[0] if cached else None

    approved: list[ApprovedImage] = []
    if query_is_safe(query):
        with new_client() as client:
            candidates = search_commons(client, query, settings.IMAGE_SEARCH_TOP_IMAGE_CANDIDATES)
            strong = [image for image in candidates if is_strong_match(query, image)]
            approved = [approve(image) for image in safe_images(client, strong)][:1]

    cache.set(key, approved, settings.IMAGE_SEARCH_CACHE_SECONDS)
    return approved[0] if approved else None
