"""
Retrieve titles for each domain in the list of top domains
"""
from multiprocessing import Process
from time import sleep
from urllib.parse import urlsplit, urlunsplit

import bs4
import requests

from mwmbl.indexer.fsqueue import FSQueue, ZstdJsonSerializer
from mwmbl.indexer.paths import DATA_DIR, DOMAINS_QUEUE_NAME, DOMAINS_TITLES_QUEUE_NAME

NUM_PROCESSES = 10


def get_redirect_no_cookies(url, max_redirects=5):
    if max_redirects == 0:
        raise RecursionError("Too many redirects")
    try:
        result = requests.get(url, allow_redirects=False, timeout=10)
    except requests.exceptions.SSLError as e:
        print("Unable to get with SSL", e)
        result = requests.get(url, allow_redirects=False, verify=False, timeout=10)
    if result.status_code // 100 == 3:
        location = result.headers['Location']
        if not location.startswith('http'):
            parsed_url = urlsplit(url)
            location = urlunsplit(parsed_url[:2] + (location, '', ''))

        return get_redirect_no_cookies(location, max_redirects=max_redirects - 1)
    return result


def get_domain_titles():
    domains_queue = FSQueue(DATA_DIR, DOMAINS_QUEUE_NAME, ZstdJsonSerializer())
    titles_queue = FSQueue(DATA_DIR, DOMAINS_TITLES_QUEUE_NAME, ZstdJsonSerializer())
    while True:
        items_id, items = domains_queue.get()
        titles = retrieve_titles(items)
        # print("Item", item)
        # print("Title", type(title))
        # print("Title item", str(title_item))
        # print("Dump", pickle.dumps(title_item))
        titles_queue.put(titles)
        domains_queue.done(items_id)
        print("Done titles", len(titles))


def retrieve_titles(items):
    titles = []
    for item in items:
        rank, domain = item
        print("Domain", domain, rank)
        status, title, url = retrieve_title(domain)
        title_item = dict(rank=rank, domain=domain, status=status, url=url, title=title)
        titles.append(title_item)
    return titles


def retrieve_title(domain):
    original_url = f"https://{domain}"
    try:
        result = get_redirect_no_cookies(original_url)
        status = result.status_code
        url = result.url
    except (RecursionError, requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout) as e:
        print("Error retrieving URL", str(e))
        status = None
        url = None

    # print("Status", status)
    if status != 200:
        title = None
    else:
        title_tag = bs4.BeautifulSoup(result.content, features="lxml").find('title')
        title = str(title_tag.string) if title_tag is not None else domain
        # print("Title", domain, title)
    return status, title, url


def run():
    for i in range(NUM_PROCESSES):
        process = Process(target=get_domain_titles)
        process.start()
        sleep(3)


if __name__ == '__main__':
    run()
