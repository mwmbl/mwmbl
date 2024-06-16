import os
from pathlib import Path

from django.conf import settings
from redis import Redis

from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.models import DomainSubmission
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import HeuristicRanker


def get_curated_domains():
    curated_domains = set(DomainSubmission.objects.filter(status="APPROVED").values_list('name', flat=True))
    return curated_domains


redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)
queued_batches = RedisURLQueue(redis, get_curated_domains)
completer = Completer()
index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
tiny_index = TinyIndex(item_factory=Document, index_path=index_path)
tiny_index.__enter__()

ranker = HeuristicRanker(tiny_index, completer)
batch_cache = BatchCache(Path(settings.DATA_PATH) / settings.BATCH_DIR_NAME)
