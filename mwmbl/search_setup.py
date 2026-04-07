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
from mwmbl.tinysearchengine.ltr_rank import LTRRanker
from mwmbl.tinysearchengine.rank import HeuristicAndWikiRanker

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

# To use the Rust XGBoost model, set MWMBL_LTR_MODEL_PATH to the path of a trained model file.
# The model must have been trained with RustXGBPipeline.save_model().
# If the env var is not set, fall back to the heuristic ranker.
_ltr_model_path = os.environ.get("MWMBL_LTR_MODEL_PATH")

if _ltr_model_path and Path(_ltr_model_path).exists():
    try:
        from mwmbl.tinysearchengine.ltr import RustXGBPipeline
        _model = RustXGBPipeline.from_model_path(_ltr_model_path)
        ranker = LTRRanker(tiny_index, completer, _model, top_n=1000, include_wiki=True, num_wiki_results=5)
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "mwmbl_rank Rust extension not available; falling back to heuristic ranker. "
            "Run 'maturin develop' to build the Rust extension."
        )
        ranker = HeuristicAndWikiRanker(tiny_index, completer)
else:
    ranker = HeuristicAndWikiRanker(tiny_index, completer)

batch_cache = BatchCache(Path(settings.DATA_PATH) / settings.BATCH_DIR_NAME)
