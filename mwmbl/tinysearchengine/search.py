import dataclasses
from logging import getLogger
from typing import Optional

from ninja import NinjaAPI, Router, Schema

from mwmbl.format import format_result
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

logger = getLogger(__name__)


SCORE_THRESHOLD = 0.25

# Module-level router used by the unified v1 API
router = Router(tags=["Search"])


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


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def _register_routes(r: Router | NinjaAPI, ranker: HeuristicRanker):
    """Register search routes on the given router or API instance."""

    @r.get(
        "",
        response=list[SearchResult],
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
                    "description": "The search query string.",
                    "schema": {"type": "string", "example": "python tutorial"},
                }
            ],
            "responses": {
                "200": {
                    "description": "A list of formatted search results.",
                    "content": {
                        "application/json": {
                            "example": [
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
                                },
                                {
                                    "url": "https://wiki.python.org/moin/BeginnersGuide/Programmers",
                                    "title": [
                                        {"value": "Python", "is_bold": True},
                                        {"value": " for Programmers — ", "is_bold": False},
                                        {"value": "Python", "is_bold": True},
                                        {"value": " Wiki", "is_bold": False},
                                    ],
                                    "extract": [
                                        {"value": "A list of ", "is_bold": False},
                                        {"value": "Python", "is_bold": True},
                                        {"value": " resources for experienced programmers.", "is_bold": False},
                                    ],
                                    "source": "mwmbl",
                                },
                            ]
                        }
                    },
                }
            },
        },
    )
    def search(request, s: str):
        results = ranker.search(s, [])
        return [format_result(result, s) for result in results]

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
                    "description": "The partial query string to complete.",
                    "schema": {"type": "string", "example": "pyth"},
                }
            ],
            "responses": {
                "200": {
                    "description": (
                        "A two-element list: the original query string and a list of suggestions."
                    ),
                    "content": {
                        "application/json": {
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
                            }
                        }
                    },
                }
            },
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
                    "description": "The search query string.",
                    "schema": {"type": "string", "example": "python tutorial"},
                }
            ],
            "responses": {
                "200": {
                    "description": "The raw query and unranked index results.",
                    "content": {
                        "application/json": {
                            "example": {
                                "query": "python tutorial",
                                "results": [
                                    {
                                        "title": "The Python Tutorial",
                                        "url": "https://docs.python.org/3/tutorial/",
                                        "extract": "Python is an easy to learn, powerful programming language.",
                                        "score": 0.92,
                                        "term": "python tutorial",
                                        "state": None,
                                    },
                                    {
                                        "title": "Python for Beginners",
                                        "url": "https://www.python.org/about/gettingstarted/",
                                        "extract": "Are you new to programming? Python is great for beginners.",
                                        "score": 0.74,
                                        "term": "python",
                                        "state": None,
                                    },
                                ],
                            }
                        }
                    },
                }
            },
        },
    )
    def raw(request, s: str):
        results = ranker.get_raw_results(s)
        return {"query": s, "results": [dataclasses.asdict(result) for result in results]}


def create_router(ranker: HeuristicRanker, version: str) -> NinjaAPI:
    """Create a standalone NinjaAPI for a specific version (used for legacy routes)."""
    api = NinjaAPI(urls_namespace=f"search-{version}")
    _register_routes(api, ranker)
    return api


def init_router(ranker: HeuristicRanker):
    """Initialise the module-level router with the given ranker (called from urls.py)."""
    _register_routes(router, ranker)
