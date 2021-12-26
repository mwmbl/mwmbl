"""
Make a curl script for testing performance
"""
import os
from itertools import islice
from urllib.parse import quote

from indexer.paths import DATA_DIR
from indexer.wiki import get_wiki_titles_and_urls

URL_TEMPLATE = "http://localhost:8000/complete?q={}"
CURL_FILE = os.path.join(DATA_DIR, "urls.curl")


def get_urls():
    titles_and_urls = get_wiki_titles_and_urls()
    for title, url in islice(titles_and_urls, 100):
        query = quote(title.lower())
        yield URL_TEMPLATE.format(query)


def run():
    with open(CURL_FILE, 'wt') as output_file:
        for url in get_urls():
            output_file.write(f'url="{url}"\n')


if __name__ == '__main__':
    run()
