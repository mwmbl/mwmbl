"""
Record which source domains link to destination domains. Each source domain is a bloom filter containing sets of
destination domains.
"""
from itertools import islice
from logging import getLogger

from django.conf import settings
from pybloomfilter import BloomFilter

from mwmbl.hn_top_domains_filtered import DOMAINS


logger = getLogger(__name__)

DOMAIN_GROUPS = [
    ('github.com', 10),
    ('en.wikipedia.org', 10),
    ('news.ycombinator.com', 10),
    ('lemmy.ml', 2),
    ('mastodon.social', 2),
    ('top', 5),
    ('other', 1),
]

TOP_DOMAINS = set(islice(DOMAINS, 4000))
OTHER_DOMAINS = set(islice(DOMAINS, 4000, 10000))


def get_bloom_filter(domain_group: str) -> BloomFilter:
    try:
        bloom_filter = BloomFilter.open(settings.DOMAIN_LINKS_BLOOM_FILTER_PATH.format(domain_group=domain_group))
    except FileNotFoundError:
        bloom_filter = BloomFilter(settings.NUM_DOMAINS_IN_BLOOM_FILTER, 1e-6,
                                   settings.DOMAIN_LINKS_BLOOM_FILTER_PATH.format(domain_group=domain_group), perm=0o666)
    return bloom_filter


class DomainLinkDatabase:
    def __init__(self):
        self.links = {}

    def __enter__(self):
        self.links = {domain_group: (get_bloom_filter(domain_group), score) for domain_group, score in DOMAIN_GROUPS}
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for bloom_filter, _ in self.links.values():
            bloom_filter.close()

    def update_domain_links(self, source: str, target: set[str]):
        if source in DOMAIN_GROUPS:
            domain_group = source
        elif source in TOP_DOMAINS:
            domain_group = 'top'
        elif source in OTHER_DOMAINS:
            domain_group = 'other'
        else:
            # This is a URL that we don't care about
            return

        logger.info(f"Updating domain links for {domain_group} with {target} links")
        bloom_filter, score = self.links[domain_group]
        bloom_filter.update(target)

    def get_domain_score(self, domain: str) -> float:
        return sum(score if domain in bloom_filter else 0 for bloom_filter, score in self.links.values())
