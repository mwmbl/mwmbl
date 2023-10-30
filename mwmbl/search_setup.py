from multiprocessing import Queue
from pathlib import Path

from django.conf import settings

from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.paths import INDEX_NAME, BATCH_DIR_NAME
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

queued_batches = Queue()
completer = Completer()
index_path = Path(settings.DATA_PATH) / INDEX_NAME
tiny_index = TinyIndex(item_factory=Document, index_path=index_path)
tiny_index.__enter__()

ranker = HeuristicRanker(tiny_index, completer)
batch_cache = BatchCache(Path(settings.DATA_PATH) / BATCH_DIR_NAME)
