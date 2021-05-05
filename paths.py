import os

HOME = os.getenv('HOME')
DATA_DIR = os.path.join(HOME, 'data', 'tinysearch')
HN_TOP_PATH = os.path.join(DATA_DIR, 'hn-top.csv')
CRAWL_PREFIX = 'crawl_'
CRAWL_GLOB = os.path.join(DATA_DIR, f"{CRAWL_PREFIX}*")
INDEX_PATH = os.path.join(DATA_DIR, 'index.tinysearch')
TEST_INDEX_PATH = os.path.join(DATA_DIR, 'index-test.tinysearch')
WIKI_DATA_PATH = os.path.join(DATA_DIR, 'enwiki-20210301-pages-articles1.xml-p1p41242.bz2')
WIKI_TITLES_PATH = os.path.join(DATA_DIR, 'abstract-titles-sorted.txt.gz')

DOMAINS_QUEUE_NAME = 'domains-queue-fs'
DOMAINS_TITLES_QUEUE_NAME = 'domains-title-queue-fs'
DOMAINS_PATH = os.path.join(DATA_DIR, 'top10milliondomains.csv.gz')
