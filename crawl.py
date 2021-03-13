"""
Crawl the web
"""
import gzip
import hashlib
import os

import pandas as pd
import requests
import justext

from paths import DATA_DIR, HN_TOP_PATH, CRAWL_PREFIX


def crawl():
    data = pd.read_csv(HN_TOP_PATH)

    for url in data['url']:
        print("Fetching", url)
        html = fetch(url)
        filename = hashlib.md5(url.encode('utf8')).hexdigest()
        path = os.path.join(DATA_DIR, f"{CRAWL_PREFIX}{filename}.html.gz")
        if os.path.isfile(path):
            print("Path already exists, skipping")

        with gzip.open(path, 'w') as output:
            output.write(html.encode('utf8'))


def fetch(url):
    page_data = requests.get(url)
    return page_data.text


if __name__ == '__main__':
    crawl()
