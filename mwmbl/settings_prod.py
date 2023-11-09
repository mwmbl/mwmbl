import os

import dj_database_url

from mwmbl.settings_common import *


SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]


STATIC_ROOT = "/app/static/"

DJANGO_VITE_ASSETS_PATH = "/front-end-build/"
DJANGO_VITE_MANIFEST_PATH = Path(DJANGO_VITE_ASSETS_PATH) / "manifest.json"
STATICFILES_DIRS = [DJANGO_VITE_ASSETS_PATH]

DATABASES = {'default': dj_database_url.config(default=os.environ["DATABASE_URL"])}

DEBUG = True    # TODO set back to False
ALLOWED_HOSTS = ["api.mwmbl.org", "mwmbl.org", "beta.mwmbl.org"]
CSRF_TRUSTED_ORIGINS = [f"https://{domain}" for domain in ALLOWED_HOSTS]


# Sendgrid email settings
EMAIL_HOST = 'smtp.sendgrid.net'
EMAIL_HOST_USER = 'apikey'
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
EMAIL_PORT = 587
EMAIL_USE_TLS = True


DATA_PATH = "/app/storage"
RUN_BACKGROUND_PROCESSES = False
NUM_PAGES = 10240000
