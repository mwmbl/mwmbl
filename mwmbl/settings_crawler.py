import os

from mwmbl.settings_dev import *

DATA_PATH = f"{os.environ['HOME']}/.mwmbl"
INDEX_NAME = "crawl-index.tinysearch"

# Index of around 400Mb = 4096b * 100_000
NUM_PAGES = 100_000

URLS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "urls-{year}-{month}.bloom")
URLS_BLOOM_FILTER_FALLBACK_PATH = str(Path(DATA_PATH) / "urls.bloom")
NUM_URLS_IN_BLOOM_FILTER = 10_000_000

DOMAIN_LINKS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "links_{domain_group}.bloom")
NUM_DOMAINS_IN_BLOOM_FILTER = 100_000

# settings_dev computed this against its own DATA_PATH of ./devdata, and a relative path in
# the crawler container resolves under the working directory rather than the volume mounted
# at DATA_PATH: the cache went into the container's writable layer, where nothing bounds it
# and a restart silently discards it. Every path derived from DATA_PATH has to be re-derived
# here, the same way settings_prod does it.
REQUEST_CACHE_PATH = f"{DATA_PATH}/request_cache"

HAS_DATABASE = False
