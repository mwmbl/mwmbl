from pathlib import Path

from ninja import NinjaAPI

from app import settings
from mwmbl.indexer.paths import INDEX_NAME
from mwmbl.tinysearchengine import search
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

api = NinjaAPI(version="1.0.0")

index_path = Path(settings.DATA_PATH) / INDEX_NAME
tiny_index = TinyIndex(item_factory=Document, index_path=index_path)
tiny_index.__enter__()

completer = Completer()
ranker = HeuristicRanker(tiny_index, completer)

search_router = search.create_router(ranker)

api.add_router("/search/", search_router)


@api.get("/hello")
def hello(request):
    return {"response": "Hello world"}
