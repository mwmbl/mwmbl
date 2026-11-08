"""Image search endpoints: live Wikimedia Commons results, each passed through the safety pipeline.

Failing closed is the contract: if anything needed to judge an image is missing, the image is not
returned, and if Commons cannot be used at all the response is a 503, never an unchecked list.
"""

import logging

from django.conf import settings
from ninja import Query, Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from mwmbl.imagesearch import pipeline
from mwmbl.imagesearch.types import ApprovedImage, ImageSourceUnavailable

logger = logging.getLogger(__name__)

router = Router(tags=["Image Search"])


class ImageResult(Schema):
    title: str
    page_url: str = Field(description="The image's page on its source, which has the full-size file and licence.")
    thumbnail_url: str = Field(description="Load this directly from the source: Mwmbl does not proxy or store images.")
    thumbnail_width: int
    thumbnail_height: int
    source: str
    license_name: str
    license_url: str
    attribution: str


class ImageSearchResponse(Schema):
    query: str
    results: list[ImageResult]


class TopImageResponse(Schema):
    query: str
    image: ImageResult | None


def to_result(image: ApprovedImage) -> ImageResult:
    return ImageResult(**image.__dict__)


@router.get(
    "",
    response=ImageSearchResponse,
    auth=None,
    summary="Image search",
    description=(
        "Search Wikimedia Commons live and return only images that pass Mwmbl's safety checks. "
        "Anything uncertain is left out, so a query can return fewer images than Commons has."
    ),
)
def image_search(request, q: str, limit: int = Query(20, ge=1, le=settings.IMAGE_SEARCH_MAX_RESULTS)):
    try:
        approved = pipeline.search_images(q, limit)
    except ImageSourceUnavailable as e:
        logger.warning("Image search unavailable: %s", e)
        raise HttpError(503, "Image search is temporarily unavailable.") from e
    return ImageSearchResponse(query=q, results=[to_result(image) for image in approved])


@router.get(
    "top/",
    response=TopImageResponse,
    auth=None,
    summary="Top image for a web search",
    description=(
        "The single approved image to show alongside web results, or `null` when the query has no "
        "strong, safe image match."
    ),
)
def top_image(request, q: str):
    try:
        image = pipeline.top_image(q)
    except ImageSourceUnavailable as e:
        logger.warning("Image search unavailable: %s", e)
        raise HttpError(503, "Image search is temporarily unavailable.") from e
    return TopImageResponse(query=q, image=to_result(image) if image else None)
