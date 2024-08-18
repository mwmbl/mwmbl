from mwmbl.settings_common import *


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-qqr#f(i3uf%m8%8u35vn=ov-uk(*8!a&1t-hxa%ev2^t1%j&sm'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


STATIC_ROOT = ""
DJANGO_VITE_ASSETS_PATH = Path(__file__).parent.parent / "front-end" / "dist"
DJANGO_VITE_MANIFEST_PATH = DJANGO_VITE_ASSETS_PATH / "manifest.json"

STATICFILES_DIRS = [str(DJANGO_VITE_ASSETS_PATH)]


DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

DATA_PATH = "./devdata"
INDEX_NAME = 'index-v2.tinysearch'

NUM_PAGES = 2560

URLS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "urls-{year}-{month}.bloom")
URLS_BLOOM_FILTER_FALLBACK_PATH = str(Path(DATA_PATH) / "urls.bloom")
NUM_URLS_IN_BLOOM_FILTER = 100_000

DOMAIN_LINKS_BLOOM_FILTER_PATH = str(Path(DATA_PATH) / "links_{domain_group}.bloom")
NUM_DOMAINS_IN_BLOOM_FILTER = 100_000
