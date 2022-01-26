"""
See how many unique URLs and root domains we have crawled.
"""
import glob
import gzip
import json
from urllib.parse import urlparse

CRAWL_GLOB = "../../data/mwmbl/b2/*/*/*/*/*/*.json.gz"


def get_urls():
    for path in glob.glob(CRAWL_GLOB):
        data = json.load(gzip.open(path))
        for item in data['items']:
            yield item['url']


def analyse_urls(urls):
    url_set = set()
    domains = set()
    count = 0
    for url in urls:
        count += 1
        url_set.add(url)
        parsed_url = urlparse(url)
        path = parsed_url.path.strip('/')
        if path == '':
            domains.add(parsed_url.netloc)

    print("Root pages crawled", sorted(domains))
    print(f"Got {len(url_set)} URLs and {len(domains)} root pages from {count} items")


def run():
    urls = get_urls()
    analyse_urls(urls)


if __name__ == '__main__':
    run()

