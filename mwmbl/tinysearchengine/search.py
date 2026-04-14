import dataclasses
from logging import getLogger

from ninja import NinjaAPI, Router

from mwmbl.format import format_result
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

logger = getLogger(__name__)


SCORE_THRESHOLD = 0.25

# Module-level router used by the unified v1 API
router = Router(tags=["Search"])


def _register_routes(r: Router | NinjaAPI, ranker: HeuristicRanker):
    """Register search routes on the given router or API instance."""

    @r.get(
        "",
        summary="Search",
        description=(
            "Search the Mwmbl index and return formatted results. "
            "Results are ranked using a heuristic ranker and filtered to a minimum relevance score. "
            "Pass the search query as the `s` query parameter."
        ),
    )
    def search(request, s: str):
        """
        Example request:
            GET /api/v1/search/?s=python+tutorial

        Example response:
            [
              {
                "url": "https://docs.python.org/3/tutorial/",
                "title": "The Python Tutorial",
                "extract": "Python is an easy to learn, powerful programming language...",
                "score": 0.92
              }
            ]
        """
        results = ranker.search(s, [])
        return [format_result(result, s) for result in results]

    @r.get(
        "/complete",
        summary="Autocomplete",
        description=(
            "Return autocomplete suggestions for a partial query string. "
            "Useful for powering search-as-you-type interfaces. "
            "Pass the partial query as the `q` query parameter."
        ),
    )
    def complete(request, q: str):
        """
        Example request:
            GET /api/v1/search/complete?q=pyth

        Example response:
            ["python", "python tutorial", "python documentation"]
        """
        return ranker.complete(q)

    @r.get(
        "/raw",
        summary="Raw search results",
        description=(
            "Return raw, unformatted search results directly from the index. "
            "Includes internal scoring fields useful for debugging and evaluation. "
            "Pass the search query as the `s` query parameter."
        ),
    )
    def raw(request, s: str):
        """
        Example request:
            GET /api/v1/search/raw?s=python+tutorial

        Example response:
            {
              "query": "python tutorial",
              "results": [
                {
                  "url": "https://docs.python.org/3/tutorial/",
                  "title": "The Python Tutorial",
                  "extract": "Python is an easy to learn...",
                  "score": 0.92,
                  "term": "python tutorial"
                }
              ]
            }
        """
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
