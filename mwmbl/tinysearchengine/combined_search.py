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
with Staan's results as additional_results, blacklist-filters the lot and ranks it.

There is no separate Wikipedia fetch: Staan already returns Wikipedia pages when they are
relevant, and evaluation found the extra fetch roughly neutral on quality (see
mwmbl/rankeval/combined-search-handover.md).

Nothing found here is written back to the search index. Super Search indexes what it finds;
whether third-party SERP results belong in our index is a separate question with its own
answer, and this endpoint does not pre-empt it.
"""

import asyncio
from logging import getLogger

from asgiref.sync import sync_to_async
from django.conf import settings
from ninja import Router
from ninja.errors import HttpError

from mwmbl.format import format_result_v2
from mwmbl.quota import (
    check_rate_limit,
    decrement_monthly_combined_search,
    increment_monthly_combined_search,
)
from mwmbl.search_auth import authenticate_user
from mwmbl.tinysearchengine.search import SearchResponse
from mwmbl.tinysearchengine.staan import get_staan_results

logger = getLogger(__name__)

router = Router(tags=["Combined Search"])


DESCRIPTION = (
    "Search the Mwmbl index and EUSP (European Search Perspective) in one request and "
    "return the ranked union.\n\n"
    "Every candidate - crawled by Mwmbl or returned by EUSP - is scored by one "
    "learning-to-rank model trained on the pooled candidate set, then "
    "diversified so a single domain cannot take the whole page. The response is the same "
    "SearXNG-compatible shape as `/api/v2/search/`.\n\n"
    "The `engine` field names the provider a result came from:\n"
    "- `mwmbl` - organically crawled by the Mwmbl crawler\n"
    "- `eusp` - returned by the European Search Perspective (EUSP) web-search API\n"
    "- `wikipedia` - a Wikipedia page from the Mwmbl index\n"
    "- `google`, `user` - originally suggested via Google, or submitted by a user\n\n"
    "Authentication is required: a search-scoped API key in `X-API-Key`, or a JWT bearer "
    "token. Obtain a key via `POST /api/v1/platform/api-keys/`. A per-user monthly quota "
    "applies; `monthly_usage` and `monthly_limit` report it on every response.\n\n"
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


def init_router(ranker) -> None:
    @router.get(
        "",
        response=SearchResponse,
        # Handled manually in the view so both an API key and a JWT work under an async
        # view - the same reason Super Search does it this way.
        auth=None,
        summary="Combined Search (Mwmbl + EUSP)",
        description=DESCRIPTION,
        openapi_extra=OPENAPI_EXTRA,
    )
    async def combined_search(request, q: str):
        user = await authenticate_user(request)

        if not await sync_to_async(check_rate_limit)(user.id):
            raise HttpError(429, "Rate limit exceeded: maximum 5 requests per second.")

        monthly_limit = settings.COMBINED_SEARCH_MONTHLY_LIMIT
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
        results = await asyncio.to_thread(ranker.search_retrieved, retrieval, staan_results)

        formatted = [format_result_v2(result, i + 1, q) for i, result in enumerate(results)]
        return SearchResponse(
            query=q,
            number_of_results=len(formatted),
            results=formatted,
            monthly_usage=monthly_usage,
            monthly_limit=monthly_limit,
        )
