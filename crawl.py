"""
Crawl the web
"""
import gzip
import hashlib
import os
import sys
from traceback import print_tb, print_exc

import pandas as pd
import requests

from paths import DATA_DIR, HN_TOP_PATH, CRAWL_PREFIX


def crawl():
    data = pd.read_csv(HN_TOP_PATH)

    for url in data['url']:
        filename = hashlib.md5(url.encode('utf8')).hexdigest()
        path = os.path.join(DATA_DIR, f"{CRAWL_PREFIX}{filename}.html.gz")
        if os.path.isfile(path):
            print("Path already exists, skipping", url)
            continue

        print("Fetching", url)
        try:
            html = fetch(url)
        except Exception:
            print_exc(file=sys.stderr)
            print("Unable to fetch", url)
            continue

        with gzip.open(path, 'wt') as output:
            output.write(url + '\n')
            output.write(html)


def fetch(url):
    page_data = requests.get(url, timeout=10)
    return page_data.text


if __name__ == '__main__':
    crawl()
