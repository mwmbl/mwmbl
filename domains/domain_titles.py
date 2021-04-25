"""
Retrieve titles for each domain in the list of top domains
"""
import pickle
from urllib.parse import urlsplit, urlunsplit

import bs4
import requests
from persistqueue import SQLiteAckQueue

from paths import DOMAINS_QUEUE_PATH, DOMAINS_TITLES_QUEUE_PATH


def get_redirect_no_cookies(url, max_redirects=5):
    if max_redirects == 0:
        raise RecursionError("Too many redirects")
    result = requests.get(url, allow_redirects=False, verify=False)
    if result.status_code // 100 == 3:
        location = result.headers['Location']
        if not location.startswith('http'):
            parsed_url = urlsplit(url)
            location = urlunsplit(parsed_url[:2] + (location, '', ''))

        return get_redirect_no_cookies(location, max_redirects=max_redirects - 1)
    return result


def get_domain_titles():
    domains_queue = SQLiteAckQueue(DOMAINS_QUEUE_PATH)
    titles_queue = SQLiteAckQueue(DOMAINS_TITLES_QUEUE_PATH)
    while True:
        item = domains_queue.get()
        print("Item", item)
        rank, domain = item
        print("Domain", domain, rank, type(domain), type(rank))
        status, title, url = retrieve_title(domain)
        print("Title", type(title))
        title_item = dict(rank=rank, domain=domain, status=status, url=url, title=title)
        print("Title item", str(title_item))
        print("Dump", pickle.dumps(title_item))
        titles_queue.put(title_item)
        domains_queue.ack(item)


def retrieve_title(domain):
    original_url = f"https://{domain}"
    try:
        result = get_redirect_no_cookies(original_url)
        status = result.status_code
        url = result.url
    except (RecursionError, requests.exceptions.ConnectionError) as e:
        print("Error retrieving URL", str(e))
        status = None
        url = None

    print("Status", status)
    if status != 200:
        title = None
    else:
        title_tag = bs4.BeautifulSoup(result.content, features="lxml").find('title')
        title = str(title_tag.string) if title_tag is not None else domain
        print("Title", domain, title)
    return status, title, url


def run():
    get_domain_titles()


if __name__ == '__main__':
    run()
