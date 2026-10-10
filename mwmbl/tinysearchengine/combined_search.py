"""Combined Search - one request over the Mwmbl index and Staan.

Pools both sources, ranks the union with a model of its own, and answers with the same
SearXNG-shaped JSON as /api/v2/search/. Authentication is required and a flat monthly quota
applies, exactly as for Super Search, which this endpoint is meant to replace.

There is deliberately no streaming and no crawling on the request path. Super Search streams
because it crawls promoted pages and follows their outbound links, which takes seconds; the
seed crawl below runs in the background instead, so the response never waits on it. Staan is the
only network call here, and it is cached, so there is nothing to stream and a plain response
is what a client actually wants.

The ranking core is three calls. Ranker.retrieve looks the query up in the index while
Staan is fetched, so the two overlap. Ranker.search_retrieved then pools the index pages
with Staan's results as additional_results, blacklist-filters the lot and ranks it, and
JevRanker re-orders the top of that with one Jev request (see jev_rank.py).

There is no separate Wikipedia fetch: Staan already returns Wikipedia pages when they are
relevant, and evaluation found the extra fetch roughly neutral on quality (see
mwmbl/rankeval/combined-search-handover.md).

Staan's results are written back to the search index, against the query's unigrams and
bigrams, exactly as Super Search indexes what it finds: a page Staan found for one person's
query then becomes a candidate for every search, plain /search/ included. The write runs
alongside ranking, and the response reports how many new pages it added.

With crawl=true the search also starts a seed crawl: the Staan results the index lacked are
crawled in the background, following links within Staan's domains, and what that adds is
read back from /new-pages - see mwmbl.indexer.seed_crawl. JWT only for now: it is free, and
API customers are billed per request.
"""

import asyncio
from logging import getLogger

from asgiref.sync import sync_to_async
from django.conf import settings
from ninja import Query, Router, Schema
from ninja.errors import HttpError
from ninja_jwt.authentication import JWTAuth
from pydantic import Field

from mwmbl import pricing
from mwmbl.format import format_result_v2
from mwmbl.indexer.index_batches import index_new_results_against_query
from mwmbl.indexer.seed_crawl import (
    CrawlOutcome,
    get_active_seed_crawl_query,
    get_seed_crawl,
    get_seed_crawl_summary,
    start_seed_crawl,
)
from mwmbl.indexer.seed_domains import top_seed_domains
from mwmbl.membership import combined_search_monthly_limit
from mwmbl.models import Membership, UserBilling
from mwmbl.quota import (
    check_rate_limit,
    decrement_monthly_combined_search,
    get_monthly_count,
    increment_monthly_combined_search,
    increment_monthly_combined_search_api_if_below,
)
from mwmbl.search_auth import authenticate_user
from mwmbl.search_setup import index_path
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import find_blacklisted_urls
from mwmbl.tinysearchengine.search import SearchResponse
from mwmbl.tinysearchengine.staan import get_staan_results

logger = getLogger(__name__)

router = Router(tags=["Combined Search"])


class CombinedSearchResponse(SearchResponse):
    pages_indexed: int = Field(
        description="Number of distinct new pages (URLs) added to the Mwmbl index from EUSP's "
        "results for this search. Pages are indexed against the query's unigrams and bigrams, "
        "so a repeated query usually adds none.",
        examples=[3],
    )
    crawl_scheduled: bool = Field(
        description="Whether this search started a seed crawl (`crawl=true`). `crawl_outcome` says why not.",
        examples=[True],
    )
    crawl_outcome: CrawlOutcome | None = Field(
        default=None,
        description="What `crawl=true` did: `scheduled` a crawl, or none because EUSP returned "
        "nothing to crawl (`no_results`), every EUSP result is already in the index "
        "(`already_indexed`), one of your crawls is still queued or running (`already_running`), "
        "or the crawl queue is full (`queue_full`). Null without `crawl=true`.",
        examples=["scheduled"],
    )
    active_crawl_query: str | None = Field(
        default=None,
        description="With `already_running`, the query of your crawl that is queued or running.",
        examples=["rust async runtimes"],
    )


class NewPage(Schema):
    url: str
    title: str
    extract: str


class SeedCrawlDomain(Schema):
    domain: str
    newly_discovered: bool = Field(description="Whether this crawl was the first to meet the domain.")
    pages_indexed: int = Field(description="New pages this crawl has added from the domain so far.", examples=[50])
    new_page_score: float = Field(
        description="`pages_indexed` as a share of the most a crawl takes from one domain "
        f"({settings.SEED_CRAWL_MAX_PAGES_PER_DOMAIN:,} pages), from 0 to 1.",
        examples=[0.5],
    )
    recent_new_page_score: float = Field(
        description="The average `new_page_score` of the domain's last "
        f"{settings.SEED_DOMAIN_RECENT_CRAWLS} finished crawls, by anyone. It falls as the crawls "
        "exhaust the domain.",
        examples=[0.3],
    )
    staan_results: int = Field(description="EUSP results for the domain across everyone's seed crawls.")
    score: float = Field(description="`recent_new_page_score` times `staan_results`: how worth crawling the domain is.")


class SeedDomainResponse(Schema):
    domain: str
    new_page_score: float = Field(
        description=f"The average share of a crawl's per-domain budget that the domain's last "
        f"{settings.SEED_DOMAIN_RECENT_CRAWLS} seed crawls found to be new pages, from 0 to 1.",
        examples=[0.3],
    )
    staan_results: int = Field(description="EUSP results for the domain across all seed crawls.")
    score: float = Field(description="`new_page_score` times `staan_results`.", examples=[1.2])


class SeedCrawlSummaryResponse(Schema):
    query: str
    status: str = Field(
        description="`queued` until the crawl starts, `crawling` while it runs, then `done`, or `failed`.",
        examples=["crawling"],
    )
    started_at: str
    finished_at: str | None
    pages_crawled: int = Field(
        description="Pages fetched so far, by every crawl of this query, including ones that added nothing."
    )
    pages_indexed: int = Field(description="Pages the crawl has added to the Mwmbl index so far.", examples=[42])
    progress: float = Field(
        description="How far through the latest crawl of this query, from 0 (queued) to 1 (done or failed).",
        examples=[0.25],
    )
    domains: list[SeedCrawlDomain] = Field(
        description="The domains EUSP returned, which the latest crawl stays within, with what it has "
        "found on each, most new pages first. Empty until the crawl starts."
    )


class SeedCrawlResponse(SeedCrawlSummaryResponse):
    pages: list[NewPage] = Field(description="Pages the crawl has added to the Mwmbl index so far.")


DESCRIPTION = (
    "Search the Mwmbl index and EUSP (European Search Perspective) in one request and "
    "return the ranked union.\n\n"
    "A learning-to-rank model trained on the pooled candidate set ranks every candidate. "
    "EUSP's results and the model's top results are then judged for relevance and quality "
    "by an LLM-based model, and ordered by that judgment and EUSP's own ranking; the rest "
    "follow in the model's order. The response is the same SearXNG-compatible shape as "
    "`/api/v2/search/`.\n\n"
    "The `engine` field names the provider a result came from:\n"
    "- `mwmbl` - organically crawled by the Mwmbl crawler\n"
    "- `eusp` - returned by the European Search Perspective (EUSP) web-search API\n"
    "- `wikipedia` - a Wikipedia page from the Mwmbl index\n"
    "- `google`, `user` - originally suggested via Google, or submitted by a user\n\n"
    "Authentication is required: a search-scoped API key in `X-API-Key`, or a JWT bearer "
    "token. With an API key every request is billed - there is no free allowance - at the "
    "same price as `/api/v2/search/` overage, within your monthly spend limit, which "
    "standard search and Combined Search share. Obtain a key via "
    "`POST /api/v1/platform/api-keys/`. With a JWT, a monthly quota set by your membership "
    "tier applies instead. `monthly_usage` and `monthly_limit` report whichever applies on "
    "every response.\n\n"
    "EUSP's results are added to the Mwmbl index; `pages_indexed` reports how many new "
    "pages that added.\n\n"
    "With `crawl=true` (JWT only), the search also starts a background crawl of the EUSP results "
    "that were not already in the Mwmbl index, following their links within the domains EUSP "
    f"returned, up to {settings.SEED_CRAWL_MAX_PAGES_PER_DOMAIN:,} pages per domain. You can have "
    "one crawl queued or running at a time. "
    "`GET /api/v2/combined-search/new-pages?q=...` lists what it has added, and "
    "`GET /api/v2/combined-search/new-pages/count?q=...` counts it.\n\n"
    "If EUSP is down or unconfigured, the request loses its extra recall, not its "
    "results: the index's results are ranked and returned as usual.\n\n"
    "**Query parameter:** `q` - the search query string (required)."
)

OPENAPI_EXTRA = {
    "parameters": [
        {
            "name": "q",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "example": "rust async runtimes"},
        },
        {
            "name": "crawl",
            "in": "query",
            "required": False,
            "schema": {"type": "boolean", "default": False},
        },
    ]
}


def index_staan_results(query: str, staan_results: list[Document]) -> set[str]:
    """Index Staan's results against the query, returning the URLs of the new pages added.

    Blacklisted domains are dropped first: index_new_results_against_query bypasses the
    blacklist check in index_documents, and the ranker's read-path filter only stops these
    pages being shown, not being written.

    Never raises: a failed index write costs the index its new pages, not the caller its
    search.
    """
    blacklisted_urls = find_blacklisted_urls(staan_results)
    allowed = [document for document in staan_results if document.url not in blacklisted_urls]
    if not allowed:
        return set()
    try:
        return index_new_results_against_query(allowed, query, str(index_path))
    except Exception:
        logger.exception("combined-search failed to index Staan results")
        return set()


def _keyed_monthly_limit(user) -> int:
    """Keyed Combined Search requests the user's spend limit allows this month.

    There is no free allowance, so a user without a spend limit is refused outright.
    """
    billing = UserBilling.objects.filter(user=user).first()
    spend_cents = billing.max_monthly_spend_cents if billing else 0
    if spend_cents <= 0:
        raise HttpError(
            402,
            "Combined Search via API key is billed from the first request. Set a monthly spend "
            "limit at https://mwmbl.org/pricing to use it.",
        )
    return pricing.combined_search_monthly_cap(spend_cents, get_monthly_count(user.id))


def init_router(ranker) -> None:
    @router.get(
        "",
        response=CombinedSearchResponse,
        # Handled manually in the view so both an API key and a JWT work under an async
        # view - the same reason Super Search does it this way.
        auth=None,
        summary="Combined Search (Mwmbl + EUSP)",
        description=DESCRIPTION,
        openapi_extra=OPENAPI_EXTRA,
    )
    async def combined_search(request, q: str, crawl: bool = False):
        user = await authenticate_user(request)

        # Checked before the quota, so a refused request is not counted.
        if crawl and request.headers.get("X-API-Key"):
            raise HttpError(403, "crawl=true is not yet available with an API key.")

        if not await sync_to_async(check_rate_limit)(user.id):
            raise HttpError(429, "Rate limit exceeded: maximum 5 requests per second.")

        # authenticate_user takes the API key over a bearer token whenever the header is
        # present, so this is how the caller authenticated. Keyed requests are billed against
        # the spend limit; web requests use the membership quota. Each has its own counter.
        if request.headers.get("X-API-Key"):
            monthly_limit = await sync_to_async(_keyed_monthly_limit)(user)
            # Billed requests are counted only when under the limit, so the counter that
            # sync_search_counts copies to Postgres, and Polar bills, never holds a refused one.
            monthly_usage = await sync_to_async(increment_monthly_combined_search_api_if_below)(user.id, monthly_limit)
            if monthly_usage is None:
                raise HttpError(
                    429,
                    f"Combined Search monthly quota exceeded: your spend limit allows {monthly_limit:,} "
                    "requests this month. Increase your monthly spend limit at https://mwmbl.org/pricing "
                    "to allow more.",
                )
        else:
            tier = await Membership.objects.filter(user=user).values_list("tier", flat=True).afirst()
            monthly_limit = combined_search_monthly_limit(tier)
            # Increment first, then check: this makes the quota check atomic under concurrent
            # requests (a check-then-increment would let racing requests both pass). Refund the
            # increment if the caller is over the limit.
            monthly_usage = await sync_to_async(increment_monthly_combined_search)(user.id)
            if monthly_usage > monthly_limit:
                await sync_to_async(decrement_monthly_combined_search)(user.id)
                raise HttpError(429, f"Combined Search monthly quota exceeded: {monthly_limit} requests per month.")

        # The index lookup doesn't need Staan's answer, so it runs while Staan is in flight
        # rather than after it. get_staan_results returns [] on failure by itself.
        retrieval, staan_results = await asyncio.gather(
            asyncio.to_thread(ranker.retrieve, q),
            asyncio.to_thread(get_staan_results, q),
        )
        # Ranking only reads what was already retrieved, so the index write can overlap it.
        results, new_staan_urls = await asyncio.gather(
            asyncio.to_thread(ranker.search_retrieved, retrieval, staan_results),
            asyncio.to_thread(index_staan_results, q, staan_results),
        )

        crawl_outcome = None
        active_crawl_query = None
        if crawl:
            crawl_outcome = await sync_to_async(start_seed_crawl)(
                user.id, q, retrieval.pages, staan_results, new_staan_urls
            )
            if crawl_outcome == CrawlOutcome.ALREADY_RUNNING:
                active_crawl_query = await sync_to_async(get_active_seed_crawl_query)(user.id)

        formatted = [format_result_v2(result, i + 1, q) for i, result in enumerate(results)]
        return CombinedSearchResponse(
            query=q,
            number_of_results=len(formatted),
            results=formatted,
            monthly_usage=monthly_usage,
            monthly_limit=monthly_limit,
            pages_indexed=len(new_staan_urls),
            crawl_scheduled=crawl_outcome == CrawlOutcome.SCHEDULED,
            crawl_outcome=crawl_outcome,
            active_crawl_query=active_crawl_query,
        )

    @router.get(
        "new-pages",
        response=SeedCrawlResponse,
        auth=JWTAuth(),
        summary="Pages added by a seed crawl",
        description="What the seed crawl started by `crawl=true` for this query has added to the "
        "Mwmbl index, so far or in all. 404 when there is no crawl for this query; records are "
        "kept for a week.",
    )
    def new_pages(request, q: str):
        record = get_seed_crawl(request.user.id, q)
        if record is None:
            raise HttpError(404, "No seed crawl for this query.")
        return record

    @router.get(
        "new-pages/count",
        response=SeedCrawlSummaryResponse,
        auth=JWTAuth(),
        summary="Count of pages added by a seed crawl",
        description="How many pages the seed crawl started by `crawl=true` for this query has "
        "added to the Mwmbl index, so far or in all, without listing them. 404 when there is no "
        "crawl for this query; records are kept for a week.",
    )
    def new_pages_count(request, q: str):
        summary = get_seed_crawl_summary(request.user.id, q)
        if summary is None:
            raise HttpError(404, "No seed crawl for this query.")
        return summary

    @router.get(
        "seed-domains",
        response=list[SeedDomainResponse],
        auth=None,
        summary="Seed domains, best first",
        description="The domains EUSP has returned for seed crawls, ordered by `score`: how likely "
        "a crawl of the domain is to add new pages to the index, times how often EUSP returns it. "
        "For crawlers deciding where to go next.",
    )
    def seed_domains(request, limit: int = Query(100, ge=1, le=1000)):
        return top_seed_domains(limit)
