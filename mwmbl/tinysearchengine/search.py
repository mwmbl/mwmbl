import dataclasses
from logging import getLogger
from typing import Optional

from ninja import NinjaAPI, Router, Schema
from ninja.errors import HttpError

from mwmbl.format import format_result, format_result_v2
from mwmbl.models import ApiKey, MwmblUser
from mwmbl.quota import check_rate_limit, get_monthly_count, increment_monthly
from mwmbl.search_auth import SearchApiKeyAuth
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

logger = getLogger(__name__)


SCORE_THRESHOLD = 0.25

# Module-level routers
router = Router(tags=["Search"])    # v1
v2_router = Router(tags=["Search"]) # v2


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class TextSegment(Schema):
    """A fragment of a title or extract, optionally highlighted."""
    value: str
    is_bold: bool

    class Config:
        json_schema_extra = {
            "examples": [
                {"value": "The ", "is_bold": False},
                {"value": "Python", "is_bold": True},
                {"value": " Tutorial", "is_bold": False},
            ]
        }


class SearchResult(Schema):
    """A single formatted search result returned by the main search endpoint."""
    url: str
    title: list[TextSegment]
    extract: list[TextSegment]
    source: str

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "url": "https://docs.python.org/3/tutorial/",
                    "title": [
                        {"value": "The ", "is_bold": False},
                        {"value": "Python", "is_bold": True},
                        {"value": " Tutorial — Python 3 documentation", "is_bold": False},
                    ],
                    "extract": [
                        {"value": "Python", "is_bold": True},
                        {"value": " is an easy to learn, powerful programming language.", "is_bold": False},
                    ],
                    "source": "mwmbl",
                }
            ]
        }


class SearxResult(Schema):
    """A single search result in SearXNG-compatible format."""
    url: str
    title: str
    content: str
    engine: str
    positions: list[int]
    score: float
    title_highlights: list[str]
    content_highlights: list[str]


class SearchResponse(Schema):
    """SearXNG-compatible search response with optional Mwmbl usage metadata."""
    query: str
    number_of_results: int
    results: list[SearxResult]
    monthly_usage: int | None = None
    monthly_limit: int | None = None


class RawDocument(Schema):
    """A raw, unformatted document as stored in the index."""
    title: str
    url: str
    extract: str
    score: Optional[float] = None
    term: Optional[str] = None
    state: Optional[int] = None

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "title": "The Python Tutorial",
                    "url": "https://docs.python.org/3/tutorial/",
                    "extract": "Python is an easy to learn, powerful programming language.",
                    "score": 0.92,
                    "term": "python tutorial",
                    "state": None,
                }
            ]
        }


class RawSearchResponse(Schema):
    """Response from the raw search endpoint."""
    query: str
    results: list[RawDocument]

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "query": "python tutorial",
                    "results": [
                        {
                            "title": "The Python Tutorial",
                            "url": "https://docs.python.org/3/tutorial/",
                            "extract": "Python is an easy to learn, powerful programming language.",
                            "score": 0.92,
                            "term": "python tutorial",
                            "state": None,
                        }
                    ],
                }
            ]
        }


# The complete endpoint returns a two-element list: [query_string, list[str]].
# Ninja cannot express this as a typed Schema, so we describe it via openapi_extra.
_COMPLETE_RESPONSE_SCHEMA = {
    "type": "array",
    "minItems": 2,
    "maxItems": 2,
    "prefixItems": [
        {
            "type": "string",
            "description": "The original partial query string, echoed back.",
            "example": "pyth",
        },
        {
            "type": "array",
            "description": (
                "List of suggestion strings. Each entry is one of:\n"
                "- A completed query term, e.g. `\"python tutorial\"`.\n"
                "- A direct-navigation URL prefixed with `\"go: \"`, "
                "e.g. `\"go: docs.python.org/3/tutorial\"`.\n"
                "- A Google fallback prefixed with `\"search: google.com \"` "
                "when no index results exist."
            ),
            "items": {"type": "string"},
            "example": [
                "go: docs.python.org/3/tutorial",
                "python",
                "python tutorial",
                "python documentation",
            ],
        },
    ],
    "examples": [
        [
            "pyth",
            [
                "go: docs.python.org/3/tutorial",
                "python",
                "python tutorial",
                "python documentation",
            ],
        ],
        [
            "xyzzy lang",
            [
                "search: google.com xyzzy lang",
                "search: google.com xyzzy language",
            ],
        ],
    ],
}


# ---------------------------------------------------------------------------
# Upgrade message helper
# ---------------------------------------------------------------------------

def _upgrade_message(tier: str) -> str:
    if tier == MwmblUser.Tier.FREE:
        return (
            "Upgrade to Starter (10,000 requests/month) or Pro (50,000 requests/month) "
            "at https://mwmbl.org/pricing."
        )
    if tier == MwmblUser.Tier.STARTER:
        return "Upgrade to Pro (50,000 requests/month) at https://mwmbl.org/pricing."
    return ""  # Pro — no upgrade available


# ---------------------------------------------------------------------------
# Route registration helpers
# ---------------------------------------------------------------------------

def _register_search_v1(r: Router | NinjaAPI, ranker: HeuristicRanker):
    """Register the v1 search endpoint: returns a plain list of SearchResult."""

    @r.get(
        "",
        response=list[SearchResult],
        auth=None,
        summary="Search",
        description=(
            "Search the Mwmbl index and return formatted results.\n\n"
            "Results are ranked using a heuristic ranker that considers title, extract, "
            "domain authority, and query-term match quality. "
            "Each result's `title` and `extract` are returned as a list of text segments "
            "where matching query terms are flagged with `is_bold: true` for easy "
            "client-side highlighting.\n\n"
            "The `source` field indicates where the result originated:\n"
            "- `mwmbl` — organically crawled by the Mwmbl crawler\n"
            "- `wikipedia` — sourced from Wikipedia\n"
            "- `google` — originally suggested via Google\n"
            "- `user` — submitted directly by a user\n\n"
            "**Query parameter:** `s` — the search query string (required)."
        ),
        openapi_extra={
            "parameters": [
                {
                    "name": "s",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "example": "python tutorial"},
                }
            ]
        },
    )
    def search(request, s: str):
        results = ranker.search(s, [])
        return [format_result(result, s) for result in results]


def _register_search_v2(r: Router | NinjaAPI, ranker: HeuristicRanker):
    """Register the v2 search endpoint: returns SearchResponse with optional quota info."""

    @r.get(
        "",
        response=SearchResponse,
        auth=None,
        summary="Search",
        description=(
            "Search the Mwmbl index and return results in SearXNG-compatible format.\n\n"
            "Unauthenticated requests are accepted but return `monthly_usage` and "
            "`monthly_limit` as `null`. To track usage against a quota, pass a valid "
            "search-scoped API key in the `X-API-Key` header. "
            "Obtain a key via `POST /api/v1/platform/api-keys/`.\n\n"
            "Results are ranked using a heuristic ranker that considers title, content, "
            "domain authority, and query-term match quality. "
            "The response follows the SearXNG JSON format: each result has a plain-text "
            "`title` and `content`, an `engine` field indicating the result source, "
            "a `score` (1/position), and SearXNG standard fields.\n\n"
            "The `engine` field indicates where the result originated:\n"
            "- `mwmbl` — organically crawled by the Mwmbl crawler\n"
            "- `wikipedia` — sourced from Wikipedia\n"
            "- `google` — originally suggested via Google\n"
            "- `user` — submitted directly by a user\n\n"
            "The response includes `monthly_usage` (requests used this month) and "
            "`monthly_limit` (your plan's monthly cap); both are `null` for "
            "unauthenticated requests.\n\n"
            "**Query parameter:** `q` — the search query string (required)."
        ),
        openapi_extra={
            "parameters": [
                {
                    "name": "q",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "example": "python tutorial"},
                }
            ]
        },
    )
    def search(request, q: str):
        raw_key = request.headers.get("X-API-Key")
        api_key = None
        if raw_key:
            api_key = SearchApiKeyAuth().authenticate(request, raw_key)
            if api_key is None:
                raise HttpError(401, "Invalid API key.")

        if api_key is not None:
            user: MwmblUser = api_key.user
            monthly_limit = MwmblUser.TIER_MONTHLY_LIMITS[user.tier]

            if not check_rate_limit(user.id):
                raise HttpError(
                    429,
                    "Rate limit exceeded: maximum 5 requests per second. Please slow down.",
                )

            current_count = get_monthly_count(user.id)
            if current_count >= monthly_limit:
                upgrade_msg = _upgrade_message(user.tier)
                msg = (
                    f"Monthly quota exceeded: your {user.get_tier_display()} plan allows "
                    f"{monthly_limit:,} requests per month and you have used {current_count:,}."
                )
                if upgrade_msg:
                    msg += f" {upgrade_msg}"
                raise HttpError(429, msg)

            monthly_usage = increment_monthly(user.id)
        else:
            monthly_limit = None
            monthly_usage = None

        raw_results = ranker.search(q, [])
        formatted = [format_result_v2(r, i + 1, q) for i, r in enumerate(raw_results)]
        return SearchResponse(
            query=q,
            number_of_results=len(formatted),
            results=formatted,
            monthly_usage=monthly_usage,
            monthly_limit=monthly_limit,
        )


def _register_common_routes(r: Router | NinjaAPI, ranker: HeuristicRanker):
    """Register the /complete and /raw endpoints (shared between v1 and v2)."""

    @r.get(
        "/complete",
        summary="Autocomplete",
        description=(
            "Return autocomplete suggestions for a partial query string.\n\n"
            "Useful for powering search-as-you-type interfaces. "
            "The response is a two-element list:\n"
            "1. The original query string (echoed back).\n"
            "2. A list of suggestion strings. Each suggestion is either:\n"
            "   - A completed query term (e.g. `\"python tutorial\"`).\n"
            "   - A direct URL prefixed with `\"go: \"` when the partial query "
            "matches a URL in the index (e.g. `\"go: docs.python.org/3/tutorial\"`).\n"
            "   - A Google search suggestion prefixed with `\"search: google.com \"` "
            "when no index results are found.\n\n"
            "**Query parameter:** `q` — the partial query string (required)."
        ),
        openapi_extra={
            "parameters": [
                {
                    "name": "q",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "example": "pyth"},
                }
            ],
            "responses": {
                "200": {
                    "description": (
                        "A two-element list: the echoed query string and a list of suggestions."
                    ),
                    "content": {
                        "application/json": {
                            "schema": _COMPLETE_RESPONSE_SCHEMA,
                            "examples": {
                                "with_index_results": {
                                    "summary": "Suggestions when index results exist",
                                    "value": [
                                        "pyth",
                                        [
                                            "go: docs.python.org/3/tutorial",
                                            "python",
                                            "python tutorial",
                                            "python documentation",
                                        ],
                                    ],
                                },
                                "no_index_results": {
                                    "summary": "Fallback to Google suggestions when no index results",
                                    "value": [
                                        "xyzzy lang",
                                        [
                                            "search: google.com xyzzy lang",
                                            "search: google.com xyzzy language",
                                        ],
                                    ],
                                },
                            },
                        }
                    },
                }
            }
        },
    )
    def complete(request, q: str):
        return ranker.complete(q)

    @r.get(
        "/raw",
        response=RawSearchResponse,
        summary="Raw search results",
        description=(
            "Return raw, unformatted search results directly from the index.\n\n"
            "Unlike the main `/search/` endpoint, results are **not** re-ranked or "
            "formatted for display. The response includes internal scoring fields "
            "that are useful for debugging, evaluation, and building custom ranking "
            "pipelines.\n\n"
            "Field descriptions for each result:\n"
            "- `title` — page title as stored in the index.\n"
            "- `url` — canonical URL of the page.\n"
            "- `extract` — short text snippet from the page body.\n"
            "- `score` — raw relevance score assigned during indexing (may be `null`).\n"
            "- `term` — the index term under which this document was stored (may be `null`).\n"
            "- `state` — integer representing the document's curation state "
            "(see `DocumentState` enum; `null` means a standard organic result).\n\n"
            "**Query parameter:** `s` — the search query string (required)."
        ),
        openapi_extra={
            "parameters": [
                {
                    "name": "s",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "example": "python tutorial"},
                }
            ]
        },
    )
    def raw(request, s: str):
        results = ranker.get_raw_results(s)
        return {"query": s, "results": [dataclasses.asdict(result) for result in results]}


def create_router(ranker: HeuristicRanker, version: str) -> NinjaAPI:
    """Create a standalone NinjaAPI for a specific version (used for legacy routes)."""
    api = NinjaAPI(urls_namespace=f"search-{version}")
    _register_search_v1(api, ranker)
    _register_common_routes(api, ranker)
    return api


def init_router(ranker: HeuristicRanker):
    """Initialise the v1 module-level router (called from urls.py)."""
    _register_search_v1(router, ranker)
    _register_common_routes(router, ranker)


def init_v2_router(ranker: HeuristicRanker):
    """Initialise the v2 module-level router (called from urls.py)."""
    _register_search_v2(v2_router, ranker)
    _register_common_routes(v2_router, ranker)
