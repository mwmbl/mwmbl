import os

HOME = os.getenv('HOME')
DATA_DIR = os.path.join(HOME, 'data', 'tinysearch')
HN_TOP_PATH = os.path.join(DATA_DIR, 'hn-top.csv')
CRAWL_PREFIX = 'crawl_'
CRAWL_GLOB = os.path.join(DATA_DIR, f"{CRAWL_PREFIX}*")
INDEX_PATH = os.path.join(DATA_DIR, 'index.sqlite3')
WIKI_DATA_PATH = os.path.join(DATA_DIR, 'enwiki-20210301-pages-articles1.xml-p1p41242.bz2')
