import os
from pathlib import Path

HOME = os.getenv('HOME')

DATA_DIR = Path(os.environ['HOME']) / 'data'
TINYSEARCH_DATA_DIR = DATA_DIR / 'tinysearch'
COMMON_CRAWL_TERMS_PATH = TINYSEARCH_DATA_DIR / 'common-craw-terms.csv'

TEST_INDEX_PATH = os.path.join(TINYSEARCH_DATA_DIR, 'index-test.tinysearch')
TEST_TERMS_PATH = os.path.join(TINYSEARCH_DATA_DIR, 'index-terms.csv')
WIKI_DATA_PATH = os.path.join(TINYSEARCH_DATA_DIR, 'enwiki-20210301-pages-articles1.xml-p1p41242.bz2')
WIKI_TITLES_PATH = os.path.join(TINYSEARCH_DATA_DIR, 'abstract-titles-sorted.txt.gz')

URLS_PATH = TINYSEARCH_DATA_DIR / 'urls.sqlite3'
DOMAINS_QUEUE_NAME = 'domains-queue-fs'
DOMAINS_TITLES_QUEUE_NAME = 'domains-title-queue-fs'
DOMAINS_PATH = os.path.join(TINYSEARCH_DATA_DIR, 'top10milliondomains.csv.gz')

LOCAL_DATA_DIR = Path(__file__).parent.parent.parent / 'data'
INDEX_PATH = LOCAL_DATA_DIR / 'index.tinysearch'
MWMBL_CRAWL_TERMS_PATH = LOCAL_DATA_DIR / 'mwmbl-crawl-terms.csv'

TOP_DOMAINS_JSON_PATH = TINYSEARCH_DATA_DIR / 'hn-top-domains.json'

MWMBL_DATA_DIR = DATA_DIR / "mwmbl"
CRAWL_GLOB = str(MWMBL_DATA_DIR / "b2") + "/*/*/*/*/*/*.json.gz"
LINK_COUNT_PATH = MWMBL_DATA_DIR / 'crawl-counts.json'

INDEX_NAME = 'index-v2.tinysearch'
BATCH_DIR_NAME = 'batches'