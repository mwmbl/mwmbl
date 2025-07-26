from mwmbl.settings_dev import *

# Disable database setup for testing
SETUP_DATABASE = False

# Use in-memory SQLite for Django ORM tests
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Test-specific settings
DATA_PATH = "/tmp/test_mwmbl_data"
INDEX_NAME = "test-index.tinysearch"
NUM_PAGES = 10

# Disable Redis URL requirement for testing
REDIS_URL = "redis://localhost:6379"

# Test bloom filter paths
URLS_BLOOM_FILTER_PATH = "/tmp/test_urls-{year}-{month}.bloom"
URLS_BLOOM_FILTER_FALLBACK_PATH = "/tmp/test_urls.bloom"
NUM_URLS_IN_BLOOM_FILTER = 1000

DOMAIN_LINKS_BLOOM_FILTER_PATH = "/tmp/test_links_{domain_group}.bloom"
NUM_DOMAINS_IN_BLOOM_FILTER = 1000

REQUEST_CACHE_PATH = "/tmp/test_request_cache"
