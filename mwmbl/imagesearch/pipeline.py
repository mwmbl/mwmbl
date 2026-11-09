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

Nothing from Commons is persisted except the approved results, which sit in the cache briefly,
and a verdict for each text and file judged, keyed by a hash (see verdict_cache). The browser
loads approved thumbnails straight from Wikimedia.
"""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import httpx
from django.conf import settings
from django.core.cache import cache

from mwmbl.imagesearch import rules, verdict_cache
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

    The model was trained on sentences, and on a bare word or two it is noise: it calls "gorilla",
    "monkey" and "ape" NSFW and "cat" uncertain. Framed as a caption it does not, so the query is
    judged as one. The deny rules, not the model, are what catch explicit search terms; the model
    is a second opinion on queries they miss. Every image still has to pass all of its own layers.
    """
    if rules.query_verdict(query) is not Verdict.SAFE:
        return False
    (verdict,) = text_classifier().classify([f"a photo of {query}"])
    return verdict is not Verdict.NSFW


def text_verdicts(texts: list[str]) -> list[Verdict]:
    """A verdict per text, running the model only on texts it has not judged before."""
    classifier = text_classifier()
    keys = [verdict_cache.cache_key("text", classifier.fingerprint, text) for text in texts]
    known = verdict_cache.get_verdicts(keys)
    unknown = {key: text for key, text in zip(keys, texts) if key not in known}
    fresh = dict(zip(unknown, classifier.classify(list(unknown.values()))))
    verdict_cache.put_verdicts(fresh)
    verdicts = known | fresh
    return [verdicts[key] for key in keys]


def pass_text_checks(images: list[SourceImage]) -> list[SourceImage]:
    ruled = [image for image in images if rules.image_verdict(image) is Verdict.SAFE]
    verdicts = text_verdicts([image.text for image in ruled])
    return [image for image, verdict in zip(ruled, verdicts) if verdict is Verdict.SAFE]


def pixel_verdict(client: httpx.Client, image: SourceImage) -> Verdict:
    data = fetch_thumbnail(client, image.thumbnail_url)
    if data is None:
        return Verdict.UNCERTAIN
    return pixel_classifier().classify_bytes(data)


def pass_pixel_checks(client: httpx.Client, images: list[SourceImage]) -> list[SourceImage]:
    classifier = pixel_classifier()
    keys = [
        verdict_cache.cache_key("pixel", classifier.fingerprint, f"{image.sha1}:{image.thumbnail_width}")
        for image in images
    ]
    known = verdict_cache.get_verdicts(keys)
    unknown = {key: image for key, image in zip(keys, images) if key not in known}
    with ThreadPoolExecutor(max_workers=THUMBNAIL_FETCH_WORKERS) as pool:
        checked = dict(zip(unknown, pool.map(lambda image: pixel_verdict(client, image), unknown.values())))

    # An UNCERTAIN verdict may only mean the thumbnail did not download or decode this time, so only
    # a verdict the model reached on a whole image is kept. Nor is one for a file that was only just
    # uploaded: Wikimedia can keep serving the previous file's thumbnail for a while after a
    # replacement, and a verdict on that must not be filed under the new file's SHA-1.
    settled_before = datetime.now(timezone.utc) - timedelta(seconds=settings.IMAGE_SEARCH_VERDICT_MIN_FILE_AGE_SECONDS)
    verdict_cache.put_verdicts(
        {
            key: verdict
            for key, verdict in checked.items()
            if verdict is not Verdict.UNCERTAIN and unknown[key].uploaded_at <= settled_before
        }
    )
    verdicts = known | checked
    return [image for key, image in zip(keys, images) if verdicts[key] is Verdict.SAFE]


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
