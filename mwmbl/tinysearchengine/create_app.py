import re
from logging import getLogger
from operator import itemgetter
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.hn_top_domains_filtered import DOMAINS
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

logger = getLogger(__name__)


SCORE_THRESHOLD = 0.25


def create(ranker: HeuristicRanker):
    app = FastAPI()
    
    # Allow CORS requests from any site
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_headers=["*"],
    )

    @app.get("/search")
    def search(s: str):
        return ranker.search(s)

    @app.get("/complete")
    def complete(q: str):
        return ranker.complete(q)

    return app
