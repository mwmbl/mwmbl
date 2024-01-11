"""
Record which source domains link to destination domains. Each source domain is a bloom filter containing sets of
destination domains.
"""
from itertools import islice

from django.conf import settings
from pybloomfilter import BloomFilter

from mwmbl.hn_top_domains_filtered import DOMAINS

URL_GROUPS = [
    'github.com',
    'en.wikipedia.org',
    'news.ycombinator.com',
    'lemmy.ml',
    'mastodon.social',
    'top',
    'other',
]

TOP_DOMAINS = set(islice(DOMAINS, 1000))
OTHER_DOMAINS = set(islice(DOMAINS, 1000, 10000))


def get_bloom_filter(url_group: str) -> BloomFilter:
    try:
        bloom_filter = BloomFilter.open(settings.DOMAIN_LINKS_BLOOM_FILTER_PATH.format(url_group=url_group))
    except FileNotFoundError:
        bloom_filter = BloomFilter(settings.NUM_DOMAINS_IN_BLOOM_FILTER, 1e-6,
                                   settings.DOMAIN_LINKS_BLOOM_FILTER_PATH.format(url_group=url_group))
    return bloom_filter


class DomainLinkDatabase:
    def __init__(self):
        self.links = {}

    def __enter__(self):
        self.links = {url_group: get_bloom_filter(url_group) for url_group in URL_GROUPS}

    def __exit__(self, exc_type, exc_val, exc_tb):
        for bloom_filter in self.links.values():
            bloom_filter.close()

    def update_domain_links(self, source: str, target: list[str]):
        if source in TOP_DOMAINS:
            url_group = 'top'
        elif source in OTHER_DOMAINS:
            url_group = 'other'
        elif source in URL_GROUPS:
            url_group = source
        else:
            # This is a URL that we don't care about
            return

        bloom_filter = self.links[url_group]
        bloom_filter.update(target)




