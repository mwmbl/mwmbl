import os

from mwmbl.settings_common import *


SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]


STATIC_ROOT = "/app/static/"

DJANGO_VITE_ASSETS_PATH = "/front-end-build/"
DJANGO_VITE_MANIFEST_PATH = Path(DJANGO_VITE_ASSETS_PATH) / "manifest.json"
STATICFILES_DIRS = [DJANGO_VITE_ASSETS_PATH]

DEBUG = False
ALLOWED_HOSTS = ["api.mwmbl.org", "mwmbl.org", "beta.mwmbl.org"]
CSRF_TRUSTED_ORIGINS = [f"https://{domain}" for domain in ALLOWED_HOSTS]


# Sendgrid email settings
EMAIL_HOST = 'smtp.sendgrid.net'
EMAIL_HOST_USER = 'apikey'
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
EMAIL_PORT = 587
EMAIL_USE_TLS = True


DATA_PATH = "/app/storage"
INDEX_NAME = 'index-v2-400G.tinysearch'

# 400GB index
NUM_PAGES = 102400000

EXTERNAL_CACHE_INDEX_NAME = 'external-cache.tinysearch'
# ~15 GB. Sized by page occupancy rather than by naive capacity, because pages are assigned
# by hash: the queries per page are Poisson-distributed, not evenly spread, so the pages
# that run over start truncating while empty ones sit unused.
#
# A 4 KB page holds ~37 documents, measured by packing the 11,693 real Wikipedia responses
# left in the old devdata/request_cache until the page overflowed. That is in line with the
# live index, which averages 35.4 documents a page, and it is worth stating how it was
# measured because a first pass using synthetic random-vocabulary text got 8, 60% low: zstd
# exploits the redundancy in real prose and there is a lot of it. Real extracts are ~154
# characters.
#
# An entry holds WIKI_FETCH_LIMIT documents a query, so at the current 5 that is ~7 cached
# queries a page. It was 3 a query (~13 queries a page) when this was first sized, and the
# capacity below is the price of serving the pool size the LTR model was trained on - see
# NUM_WIKI_RESULTS - rather than a choice made here.
#
# At an average load of L queries per page the fraction still resident is E[min(K, 7)] / L
# for K ~ Poisson(L), which gives, for one provider:
#
#     L = 3.4  ->  99% retained  ->  12.7M distinct queries
#     L = 5.0  ->  95% retained  ->  18.6M
#     L = 6.1  ->  90% retained  ->  22.9M
#     L = 7    ->  85% retained  ->  26.3M   (the naive number)
#
# A query cached from several providers holds one entry per provider, so divide by the
# number of providers: with two, ~15M queries at 99% and ~24M at 90%. Longer extracts fit
# fewer per page, so a provider more verbose than Wikipedia costs more than its share.
EXTERNAL_CACHE_NUM_PAGES = 3_750_000

URLS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "urls-{year}-{month}.bloom")
URLS_BLOOM_FILTER_FALLBACK_PATH = str(Path(DATA_PATH) / "urls.bloom")
NUM_URLS_IN_BLOOM_FILTER = 200_000_000

DOMAIN_LINKS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "links_{domain_group}.bloom")
NUM_DOMAINS_IN_BLOOM_FILTER = 100_000_000

REQUEST_CACHE_PATH = f"{DATA_PATH}/request_cache"
