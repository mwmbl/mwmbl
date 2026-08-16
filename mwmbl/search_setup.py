import os
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from redis import Redis

from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist
from mwmbl.models import DomainSubmission
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import LTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
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

# Pull the blacklist snapshot in at worker startup rather than letting the first query
# pay for the ~11 MB Redis read. Subsequent refreshes happen on a daemon thread.
snapshot_blacklist = get_snapshot_blacklist()

# Hand the snapshot to the URL queue too. Left to its own default it would build a
# get_default_blacklist_provider(), and POST /crawler/batches/new calls get_batch() ->
# is_domain_blacklisted(), which would download and parse the remote blocklists inside a
# request handler and pin ~156 MB of domain strings in each of the (cpu_count * 2 + 1)
# gunicorn workers - the cost the snapshot exists to avoid. The processes that legitimately
# need the full provider (update_urls, update_batches, the standalone crawler in crawl.py,
# whose Redis has no published snapshot) construct their own queue and are unaffected.
queued_batches = RedisURLQueue(redis, get_curated_domains, blacklist_provider=snapshot_blacklist)

completer = Completer()
index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
tiny_index = TinyIndex(item_factory=Document, index_path=index_path)
tiny_index.__enter__()

ltr_model = RustXGBPipeline.from_model_path(str(settings.RUST_MODEL_PATH))
# Diversity is applied by the wrapping MMRRanker, which demotes (rather than drops)
# same-domain / near-duplicate results. Unwrap to disable diversity.
ranker = MMRRanker(LTRRanker(tiny_index, completer, ltr_model, include_wiki=True, num_wiki_results=3))

batch_cache = BatchCache(Path(settings.DATA_PATH) / settings.BATCH_DIR_NAME)
