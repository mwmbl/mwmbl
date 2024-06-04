"""
See how many unique URLs and root domains we have crawled.
"""
import glob
import gzip
import json
from collections import defaultdict, Counter
from urllib.parse import urlparse

from mwmbl.crawler import HashedBatch
from mwmbl.indexer import CRAWL_GLOB, MWMBL_DATA_DIR


def get_urls():
    for path in glob.glob(CRAWL_GLOB):
        data = json.load(gzip.open(path))
        batch = HashedBatch.parse_obj(data)
        user = batch.user_id_hash
        for item in batch.items:
            if item.content is not None:
                for url in item.content.links:
                    yield user, url


def analyse_urls(urls):
    url_set = defaultdict(list)
    domains = set()
    for user, url in urls:
        url_set[url].append(user)

        parsed_url = urlparse(url)
        path = parsed_url.path.strip('/')
        if path == '':
            domains.add(parsed_url.netloc)

    count = sum(len(x) for x in url_set.values())
    print("Root pages crawled", sorted(domains))
    find_worst_pages(url_set)
    print(f"Got {len(url_set)} URLs and {len(domains)} root pages from {count} items")
    url_list_size = len(json.dumps(list(url_set.keys())))
    print("Length of all URLs", url_list_size)


def find_worst_pages(url_set):
    worst = sorted(((len(users), url) for url, users in url_set.items()), reverse=True)[:50]
    for count, url in worst:
        print("Worst", count, url, Counter(url_set[url]))


def run():
    urls = get_urls()
    analyse_urls(urls)


if __name__ == '__main__':
    run()

