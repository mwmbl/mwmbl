from mwmbl.settings_common import *

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

DATA_PATH = "./devdata"
RUN_BACKGROUND_PROCESSES = False
NUM_PAGES = 2560

