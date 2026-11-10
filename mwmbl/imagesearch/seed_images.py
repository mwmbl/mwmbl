"""Seed Search's images: Wikimedia Commons files shown with Combined Search's web results (mwmbl/mwmbl#610).

A search runs in two phases, each alongside the part of web search it doesn't depend on:

1. start(), called before Staan is fetched: the rules gate on the query, the Commons search, then the NSFW
   scores for the candidates, in a request of their own that carries no query.
2. finish(), called once Staan has answered, alongside web ranking: one request with the query's image need
   (given Staan's top titles) and every candidate's relevance, which doesn't wait for the NSFW scores. Then the
   selection: an image passes if its NSFW score is below the threshold, the query's need reaches the gate and
   its relevance the minimum, and the images that pass are returned most relevant first.

It fails closed: anything that goes wrong, or doesn't finish within IMAGE_SEARCH_GRACE_SECONDS of web ranking,
is `unavailable`, with no images, and never delays or costs the web results.

With no Staan results the need question, measured with them, can't be asked, so the outcome is `unavailable`;
one is enough, as the relevance question was also measured with Staan's top result alone. Images are never
stored or logged together with the query.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from logging import WARNING, getLogger

from django.conf import settings

from mwmbl.imagesearch import commons, judge, rules
from mwmbl.imagesearch.prompt import MIN_RELEVANCE, NEED_THRESHOLD, NSFW_THRESHOLD
from mwmbl.imagesearch.types import SourceImage, Verdict
from mwmbl.tinysearchengine.indexer import Document

logger = getLogger(__name__)
# httpx logs every request's URL at INFO, and the Commons search URL holds the query.
getLogger("httpx").setLevel(WARNING)

# One client for the process, so its pooled connections to Commons are reused across searches.
client = commons.new_client()
# Image work has threads of its own, so that threads left blocked on Commons or Redis after a missed deadline
# can never hold up web search's.
executor = ThreadPoolExecutor(settings.IMAGE_SEARCH_THREADS, thread_name_prefix="seed-images")
# Files nominated for deletion are still on Commons while the community decides.
DELETION_CATEGORY_PREFIXES = (
    "Deletion requests",
    "Candidates for speedy deletion",
    "Copyright violations",
    "Media without a license",
    "Media missing permission",
)


class ImageOutcome(StrEnum):
    SHOWN = "shown"
    QUERY_REFUSED = "query_refused"
    NOT_NEEDED = "not_needed"
    NONE_RELEVANT = "none_relevant"
    UNAVAILABLE = "unavailable"


class NoImages(Exception):
    """The search was decided before any image was judged."""

    def __init__(self, outcome: ImageOutcome):
        super().__init__(outcome)
        self.outcome = outcome


@dataclass(frozen=True)
class RankedImage:
    image: SourceImage
    relevance: float


@dataclass(frozen=True)
class ImageSelection:
    outcome: ImageOutcome
    images: list[RankedImage]


UNAVAILABLE = ImageSelection(ImageOutcome.UNAVAILABLE, [])


@dataclass(frozen=True)
class ImageSearch:
    """Phase 1, in flight."""

    query: str
    candidates: asyncio.Future
    nsfw_scores: asyncio.Future


def start(query: str) -> ImageSearch:
    """Begin phase 1 in the background. Call it from the request's event loop, then pass the result to finish."""
    candidates = asyncio.get_running_loop().run_in_executor(executor, find_candidates, query)
    nsfw_scores = asyncio.create_task(score_nsfw(candidates))
    for future in (candidates, nsfw_scores):
        future.add_done_callback(retrieve_outcome)
    return ImageSearch(query, candidates, nsfw_scores)


async def finish(search: ImageSearch, staan_results: list[Document], web_ranking: asyncio.Future) -> ImageSelection:
    """Phase 2 and the selection. Call it once Staan has answered, with web ranking already running as a task or
    future, which this waits for. Never raises."""
    selection = asyncio.create_task(select_images(search, staan_results))
    selection.add_done_callback(retrieve_outcome)
    await asyncio.wait([web_ranking])
    try:
        return await asyncio.wait_for(asyncio.shield(selection), settings.IMAGE_SEARCH_GRACE_SECONDS)
    except TimeoutError:
        # The threads underneath can't be stopped, and may be blocked on Redis for its socket timeout; they
        # finish on their own, NSFW scores still reach the cache, and nothing waits for them.
        logger.warning("Images missed their deadline")
        return UNAVAILABLE


def retrieve_outcome(future: asyncio.Future) -> None:
    # finish may give up on phase 1, or never see one of its failures; retrieving it here stops asyncio
    # reporting it as an unhandled error.
    if not future.cancelled():
        future.exception()


def find_candidates(query: str) -> list[SourceImage]:
    if rules.query_verdict(query) == Verdict.NSFW:
        raise NoImages(ImageOutcome.QUERY_REFUSED)
    # Without the judge no image can be shown, so Commons isn't asked either.
    if not settings.JEV_API_KEY:
        raise NoImages(ImageOutcome.UNAVAILABLE)
    found = commons.search_commons(client, query, settings.IMAGE_SEARCH_CANDIDATES)
    settled_before = datetime.now(timezone.utc) - settings.IMAGE_SEARCH_MIN_REVISION_AGE
    return [image for image in found if is_eligible(image, settled_before)]


def is_eligible(image: SourceImage, settled_before: datetime) -> bool:
    denied_files = settings.IMAGE_SEARCH_DENIED_FILES
    # Lower case on both sides, so a SHA-1 matches however it was pasted; a URL then over-matches, never under.
    denied = image.sha1.lower() in denied_files or image.page_url.lower() in denied_files
    settled = max(image.revised_at, image.uploaded_at) <= settled_before
    nominated = any(category.startswith(DELETION_CATEGORY_PREFIXES) for category in image.categories)
    return settled and not denied and not nominated and rules.image_verdict(image) == Verdict.SAFE


async def score_nsfw(candidates: asyncio.Future) -> list[float]:
    images = await candidates
    if not images:
        return []
    return await asyncio.get_running_loop().run_in_executor(executor, judge.nsfw_scores, images)


async def select_images(search: ImageSearch, staan_results: list[Document]) -> ImageSelection:
    try:
        candidates = await search.candidates
        if not candidates:
            return ImageSelection(ImageOutcome.NONE_RELEVANT, [])
        if not staan_results:
            return UNAVAILABLE
        loop = asyncio.get_running_loop()
        judged = loop.run_in_executor(executor, judge.image_scores, search.query, staan_results, candidates)
        (need, relevance), nsfw_scores = await asyncio.gather(judged, search.nsfw_scores)
        return select(candidates, need, relevance, nsfw_scores)
    except NoImages as e:
        return ImageSelection(e.outcome, [])
    except Exception as e:
        # Only the type: a message can embed the query.
        logger.warning("Images unavailable: %s", type(e).__name__)
        return UNAVAILABLE


def select(candidates: list[SourceImage], need: float, relevance: list[float], nsfw: list[float]) -> ImageSelection:
    if need < NEED_THRESHOLD:
        return ImageSelection(ImageOutcome.NOT_NEEDED, [])
    passing = [
        RankedImage(image, image_relevance)
        for image, image_relevance, nsfw_score in zip(candidates, relevance, nsfw, strict=True)
        if nsfw_score < NSFW_THRESHOLD and image_relevance >= MIN_RELEVANCE
    ]
    if not passing:
        return ImageSelection(ImageOutcome.NONE_RELEVANT, [])
    # Stable, so equally relevant images keep Commons' order.
    ranked = sorted(passing, key=lambda ranked_image: -ranked_image.relevance)
    return ImageSelection(ImageOutcome.SHOWN, ranked)
