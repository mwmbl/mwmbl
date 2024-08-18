import os

from mwmbl.settings_dev import *


DATA_PATH = f"{os.environ['HOME']}/.mwmbl"
INDEX_NAME = 'crawl-index.tinysearch'

# Index of around 400Mb = 4096b * 100_000
NUM_PAGES = 100_000

URLS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "urls-{year}-{month}.bloom")
URLS_BLOOM_FILTER_FALLBACK_PATH = str(Path(DATA_PATH) / "urls.bloom")
NUM_URLS_IN_BLOOM_FILTER = 10_000_000

DOMAIN_LINKS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "links_{domain_group}.bloom")
NUM_DOMAINS_IN_BLOOM_FILTER = 100_000

SETUP_DATABASE = False
