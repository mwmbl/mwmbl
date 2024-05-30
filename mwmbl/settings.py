import os
import re
from pathlib import Path


DATA_DIR = Path(os.environ['HOME']) / 'data' / 'tinysearch'
ALL_DOMAINS_PATH = DATA_DIR / 'hn-top-domains.csv'
TOP_DOMAINS_PATH = '../hn_top_domains_filtered.py'

MIN_COUNT = 10
PROBABILITY_THRESHOLD = 0.8
DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://username:password@localhost/dbname")

APPLICATION_KEY = os.environ.get('MWMBL_APPLICATION_KEY', 'SECRETAPPLICATIONKEY')
KEY_ID = os.environ.get('MWMBL_KEY_ID', 'SECRETKEYID')
ENDPOINT_URL = os.environ.get('MWMBL_ENDPOINT_URL', 'https://s3.us-west-004.backblazeb2.com')
BUCKET_NAME = os.environ.get('MWMBL_BUCKET_NAME', 'mwmbl-crawl')
MAX_BATCH_SIZE = 100
USER_ID_LENGTH = 36
PUBLIC_USER_ID_LENGTH = 64
VERSION = 'v1'
DATE_REGEX = re.compile(r'\d{4}-\d{2}-\d{2}')
PUBLIC_URL_PREFIX = f'https://f004.backblazeb2.com/file/{BUCKET_NAME}/'
FILE_NAME_SUFFIX = '.json.gz'

NUM_TITLE_CHARS = 65
NUM_EXTRACT_CHARS = 155

SCORE_FOR_ROOT_PATH = 0.1
SCORE_FOR_DIFFERENT_DOMAIN = 1.0
SCORE_FOR_SAME_DOMAIN = 0.01
EXTRA_LINK_MULTIPLIER = 0.001
UNKNOWN_DOMAIN_MULTIPLIER = 0.001
EXCLUDED_DOMAINS = {'web.archive.org', 'forums.giantitp.com', 'www.crutchfield.com', 'plus.google.com', 'www.lukas-renggli.ch'}
DOMAIN_BLACKLIST_REGEX = re.compile(r"porn|xxx|adult|jksu\.org|lwhyl\.org$|rgcd\.cn$|hzqwyou\.cn$|omgoat\.org$|pussyboy\.net$")
CORE_DOMAINS = {
    'github.com',
    'en.wikipedia.org',
    'stackoverflow.com',
    'docs.google.com',
    'programmers.stackexchange.com',
    'developer.mozilla.org',
    'arxiv.org',
    'www.python.org',
    'news.ycombinator.com',
}

BLACKLIST_DOMAINS_URL = "https://get.domainsblacklists.com/blacklist.txt"
