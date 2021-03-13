import os

HOME = os.getenv('HOME')
DATA_DIR = os.path.join(HOME, 'data', 'tinysearch')
HN_TOP_PATH = os.path.join(DATA_DIR, 'hn-top.csv')
CRAWL_PREFIX = 'crawl_'
CRAWL_GLOB = os.path.join(DATA_DIR, f"{CRAWL_PREFIX}*")
