"""
Analyse crawl data to find the most popular links
"""
import glob
import gzip
import json
from collections import defaultdict
from urllib.parse import urlparse

from analyse.analyse_crawled_domains import CRAWL_GLOB


def get_urls():
    for path in glob.glob(CRAWL_GLOB):
        data = json.load(gzip.open(path))
        for item in data['items']:
            url = item['url']
            domain = urlparse(url).hostname
            for link in item['links']:
                yield domain, link


def collect_links(urls):
    links = defaultdict(set)
    for url, link in urls:
        links[link].add(url)
    return links


def run():
    url_links = get_urls()
    collected = collect_links(url_links)
    top = sorted(collected.items(), key=lambda x: len(x[1]), reverse=True)[:1000]
    for url, items in top:
        print("URL", url, len(items))


if __name__ == '__main__':
    run()
