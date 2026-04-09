import os
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from redis import Redis

from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.models import DomainSubmission
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import LTRRanker
from mwmbl.tinysearchengine.rank import DomainLimitingRanker, HeuristicAndWikiRanker

CURATED_DOMAINS_CACHE_KEY = "curated-domains"
CURATED_DOMAINS_CACHE_TIMEOUT = 300


def get_curated_domains() -> set[str]:
    curated_domains = cache.get(CURATED_DOMAINS_CACHE_KEY)
    if curated_domains is None:
        curated_domains = set(DomainSubmission.objects.filter(status="APPROVED").values_list('name', flat=True))
        cache.set(CURATED_DOMAINS_CACHE_KEY, curated_domains, CURATED_DOMAINS_CACHE_TIMEOUT)
    return curated_domains


redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)
queued_batches = RedisURLQueue(redis, get_curated_domains)
completer = Completer()
index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
tiny_index = TinyIndex(item_factory=Document, index_path=index_path)
tiny_index.__enter__()

_model = RustXGBPipeline.from_model_path(str(settings.RUST_MODEL_PATH))
ranker = DomainLimitingRanker(LTRRanker(tiny_index, completer, _model, include_wiki=True, num_wiki_results=3))

batch_cache = BatchCache(Path(settings.DATA_PATH) / settings.BATCH_DIR_NAME)
