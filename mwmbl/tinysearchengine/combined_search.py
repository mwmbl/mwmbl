"""Combined Search - one request over the Mwmbl index and Staan.

Pools both sources, ranks the union with a model of its own, and answers with the same
SearXNG-shaped JSON as /api/v2/search/. Authentication is required and a flat monthly quota
applies, exactly as for Super Search, which this endpoint is meant to replace.

There is deliberately no streaming and no crawling here. Super Search streams because it
crawls promoted pages and follows their outbound links, which takes seconds; Staan is the
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
"""

import asyncio
from logging import getLogger

from asgiref.sync import sync_to_async
from ninja import Router
from ninja.errors import HttpError
from pydantic import Field

from mwmbl.format import format_result_v2
from mwmbl.indexer.index_batches import index_results_against_query
from mwmbl.membership import combined_search_monthly_limit
from mwmbl.models import Membership
from mwmbl.quota import (
    check_rate_limit,
    decrement_monthly_combined_search,
    increment_monthly_combined_search,
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
    "Authentication is required with a JWT bearer token. A per-user monthly quota applies, "
    "set by the user's membership tier; `monthly_usage` and `monthly_limit` report it on "
    "every response. Access with an API key (`X-API-Key`) is not available yet.\n\n"
    "EUSP's results are added to the Mwmbl index; `pages_indexed` reports how many new "
    "pages that added.\n\n"
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
        }
    ]
}


def index_staan_results(query: str, staan_results: list[Document]) -> int:
    """Index Staan's results against the query, returning the number of new pages added.

    Blacklisted domains are dropped first: index_results_against_query bypasses the
    blacklist check in index_documents, and the ranker's read-path filter only stops these
    pages being shown, not being written.

    Never raises: a failed index write costs the index its new pages, not the caller its
    search.
    """
    blacklisted_urls = find_blacklisted_urls(staan_results)
    allowed = [document for document in staan_results if document.url not in blacklisted_urls]
    if not allowed:
        return 0
    try:
        return index_results_against_query(allowed, query, str(index_path))
    except Exception:
        logger.exception("combined-search failed to index Staan results")
        return 0


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
    async def combined_search(request, q: str):
        user = await authenticate_user(request)
        # authenticate_user takes the API key over a bearer token whenever the header is
        # present, so this is how the caller authenticated. Combined Search costs us on
        # every request, so it has no free API tier; keyed access comes with metered billing.
        if request.headers.get("X-API-Key"):
            raise HttpError(402, "Combined Search via API key requires a paid plan, which is not available yet.")

        if not await sync_to_async(check_rate_limit)(user.id):
            raise HttpError(429, "Rate limit exceeded: maximum 5 requests per second.")

        tier = await Membership.objects.filter(user=user).values_list("tier", flat=True).afirst()
        monthly_limit = combined_search_monthly_limit(tier)
        # Increment first, then check: this makes the quota check atomic under concurrent
        # requests (a check-then-increment would let racing requests both pass). Refund the
        # increment if the caller is over the limit.
        monthly_usage = await sync_to_async(increment_monthly_combined_search)(user.id)
        if monthly_usage > monthly_limit:
            await sync_to_async(decrement_monthly_combined_search)(user.id)
            raise HttpError(
                429,
                f"Combined Search monthly quota exceeded: {monthly_limit} requests per month "
                f"and you have used {monthly_usage - 1}.",
            )

        # The index lookup doesn't need Staan's answer, so it runs while Staan is in flight
        # rather than after it. get_staan_results returns [] on failure by itself.
        retrieval, staan_results = await asyncio.gather(
            asyncio.to_thread(ranker.retrieve, q),
            asyncio.to_thread(get_staan_results, q),
        )
        # Ranking only reads what was already retrieved, so the index write can overlap it.
        results, pages_indexed = await asyncio.gather(
            asyncio.to_thread(ranker.search_retrieved, retrieval, staan_results),
            asyncio.to_thread(index_staan_results, q, staan_results),
        )

        formatted = [format_result_v2(result, i + 1, q) for i, result in enumerate(results)]
        return CombinedSearchResponse(
            query=q,
            number_of_results=len(formatted),
            results=formatted,
            monthly_usage=monthly_usage,
            monthly_limit=monthly_limit,
            pages_indexed=pages_indexed,
        )
