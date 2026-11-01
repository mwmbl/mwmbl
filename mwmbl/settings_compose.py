from mwmbl.settings_common import *

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

DJANGO_VITE_ASSETS_PATH = "/front-end-build/"
DJANGO_VITE_MANIFEST_PATH = Path(DJANGO_VITE_ASSETS_PATH) / "manifest.json"
STATICFILES_DIRS = [DJANGO_VITE_ASSETS_PATH]
STATIC_URL = 'static/'
STATIC_ROOT = "/static/"

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1", 'host.docker.internal']

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DATA_PATH = "/private"
INDEX_NAME = "index-v2.tinysearch"

NUM_PAGES = 2560

EXTERNAL_CACHE_INDEX_NAME = "external-cache.tinysearch"
EXTERNAL_CACHE_NUM_PAGES = 2560

URLS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "urls-{year}-{month}.bloom")
URLS_BLOOM_FILTER_FALLBACK_PATH = str(Path(DATA_PATH) / "urls.bloom")
NUM_URLS_IN_BLOOM_FILTER = 100_000

DOMAIN_LINKS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "links_{domain_group}.bloom")
NUM_DOMAINS_IN_BLOOM_FILTER = 100_000

REQUEST_CACHE_PATH = f"{DATA_PATH}/request_cache"
