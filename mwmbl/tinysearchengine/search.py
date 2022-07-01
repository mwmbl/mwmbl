from logging import getLogger

from fastapi import APIRouter

from mwmbl.tinysearchengine.rank import HeuristicRanker

logger = getLogger(__name__)


SCORE_THRESHOLD = 0.25


def create_router(ranker: HeuristicRanker) -> APIRouter:
    router = APIRouter(prefix="/search", tags=["search"])

    @router.get("")
    def search(s: str):
        return ranker.search(s)

    @router.get("/complete")
    def complete(q: str):
        return ranker.complete(q)

    return router
