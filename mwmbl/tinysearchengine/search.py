import dataclasses
from logging import getLogger

from ninja import NinjaAPI

from mwmbl.format import format_result
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.rank import HeuristicRanker
from mwmbl.utils import cache_control

logger = getLogger(__name__)


SCORE_THRESHOLD = 0.25


def create_router(ranker: HeuristicRanker, version: str) -> NinjaAPI:
    router = NinjaAPI(urls_namespace=f"search-{version}")

    @router.get("")
    @cache_control(max_age=3600)
    def search(request, s: str):
        results = ranker.search(s, [])
        return [format_result(result, s) for result in results]

    @router.get("/complete")
    def complete(request, q: str):
        return ranker.complete(q)

    @router.get("/raw")
    def raw(request, s: str):
        results = ranker.get_raw_results(s)
        # Convert dataclass to JSON serializable
        return {"query": s, "results": [dataclasses.asdict(result) for result in results]}

    return router
