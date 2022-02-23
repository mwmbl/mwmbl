"""
Analyse crawl data to find the most popular links
"""
import glob
import gzip
import json
from collections import defaultdict
from urllib.parse import urlparse

from mwmbl.indexer.paths import CRAWL_GLOB, LINK_COUNT_PATH


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
    link_counts = {url: len(links) for url, links in collected.items()}
    with open(LINK_COUNT_PATH, 'w') as output_file:
        json.dump(link_counts, output_file, indent=2)


if __name__ == '__main__':
    run()
