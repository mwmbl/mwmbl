from logging import getLogger

from ninja import NinjaAPI

from mwmbl.format import format_result
from mwmbl.tinysearchengine.rank import HeuristicRanker

logger = getLogger(__name__)


SCORE_THRESHOLD = 0.25


def create_router(ranker: HeuristicRanker, version: str) -> NinjaAPI:
    router = NinjaAPI(urls_namespace=f"search-{version}")

    @router.get("")
    def search(request, s: str):
        results = ranker.search(s, [])
        return [format_result(result, s) for result in results]

    @router.get("/complete")
    def complete(request, q: str):
        return ranker.complete(q)

    return router
