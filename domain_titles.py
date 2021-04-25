"""
Retrieve titles for each domain in the list of top domains
"""
import csv
import gzip
import os
from urllib.parse import urlsplit, urlunsplit

import bs4
import requests

from paths import DATA_DIR

DOMAINS_PATH = os.path.join(DATA_DIR, 'top10milliondomains.csv.gz')
TITLES_PATH = os.path.join(DATA_DIR, 'top-domains-titles.sqlite')


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
    with gzip.open(DOMAINS_PATH, 'rt') as domains_file:
        reader = csv.reader(domains_file)
        next(reader)
        for rank, domain, _ in reader:
            print("Domain", domain)
            original_url = f"https://{domain}"
            try:
                result = get_redirect_no_cookies(original_url)
                status = result.status_code
            except RecursionError as e:
                print("Error retrieving URL", str(e))
                status = None
            print("Status", status)

            if status != 200:
                title = None
            else:
                title_tag = bs4.BeautifulSoup(result.content, features="lxml").find('title')
                title = title_tag.string if title_tag is not None else domain
                print("Title", rank, domain, title)
            yield dict(rank=rank, domain=domain, status=status, url=result.url, title=title)


def save_domain_titles(domain_titles):
    with gzip.open(TITLES_PATH, 'wt') as titles_file:
        writer = csv.DictWriter(titles_file, ['rank', 'domain', 'status', 'url', 'title'])
        writer.writeheader()
        for row in domain_titles:
            writer.writerow(row)
            titles_file.flush()


def run():
    domain_titles = get_domain_titles()
    save_domain_titles(domain_titles)


if __name__ == '__main__':
    run()
