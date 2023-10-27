from mwmbl.settings_common import *


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-qqr#f(i3uf%m8%8u35vn=ov-uk(*8!a&1t-hxa%ev2^t1%j&sm'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

DATA_PATH = "./devdata"
RUN_BACKGROUND_PROCESSES = False
NUM_PAGES = 2560

