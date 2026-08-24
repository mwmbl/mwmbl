from mwmbl.settings_dev import *

HAS_DATABASE = False

# PostgreSQL is required for ArrayField.
# Set DATABASE_URL in the environment before running tests, e.g.:
#   DATABASE_URL="postgres://user@/mwmbl_test" uv run pytest
# DATABASES is inherited from settings_common (via settings_dev) and reads DATABASE_URL.

# Test-specific settings
DATA_PATH = "/tmp/test_mwmbl_data"
INDEX_NAME = "index-v2.tinysearch"
NUM_PAGES = 10

# Use fakeredis for cache so tests don't need a real Redis server
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Test bloom filter paths
URLS_BLOOM_FILTER_PATH = "/tmp/test_urls-{year}-{month}.bloom"
URLS_BLOOM_FILTER_FALLBACK_PATH = "/tmp/test_urls.bloom"
NUM_URLS_IN_BLOOM_FILTER = 1000

DOMAIN_LINKS_BLOOM_FILTER_PATH = "/tmp/test_links_{domain_group}.bloom"
NUM_DOMAINS_IN_BLOOM_FILTER = 1000

REQUEST_CACHE_PATH = "/tmp/test_request_cache"

# The front end is not built when running the tests, so there is no Vite manifest to resolve assets
# against. In dev mode django-vite points at the dev server instead of reading the manifest, which
# lets views that render the base template be tested.
DJANGO_VITE_DEV_MODE = True
