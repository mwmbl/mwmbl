from random import Random
from urllib.parse import urlparse

from redis import Redis

from mwmbl.crawler.domains import DomainLinkDatabase, TOP_DOMAINS
from mwmbl.crawler.urls import FoundURL
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.settings import CORE_DOMAINS

random = Random(1)


DOMAIN_URLS_KEY = "domain-urls-{domain}"
DOMAIN_SCORE_KEY = "domain-scores"


MAX_URLS_PER_CORE_DOMAIN = 1000
MAX_URLS_PER_TOP_DOMAIN = 100
MAX_URLS_PER_OTHER_DOMAIN = 5
MAX_OTHER_DOMAINS = 10000

MAX_BATCH_URLS_PER_CORE_DOMAIN = 100
MAX_BATCH_URLS_PER_TOP_DOMAIN = 10
MAX_BATCH_URLS_PER_OTHER_DOMAIN = 1

BATCH_SIZE = 100


def get_domain_max_urls(domain: str):
    if domain in CORE_DOMAINS:
        return MAX_URLS_PER_CORE_DOMAIN
    elif domain in TOP_DOMAINS:
        return MAX_URLS_PER_TOP_DOMAIN
    else:
        return MAX_URLS_PER_OTHER_DOMAIN


class RedisURLQueue:
    def __init__(self, redis: Redis):
        self.redis = redis

    def queue_urls(self, found_urls: list[FoundURL]):
        with DomainLinkDatabase() as link_db:
            for url in found_urls:
                domain = urlparse(url.url).netloc
                url_score = 1/len(url.url)
                domain_score = link_db.get_domain_score(domain) + url_score
                max_urls = get_domain_max_urls(domain)
                self.redis.zadd(DOMAIN_URLS_KEY.format(domain=domain), {url.url: url_score})
                self.redis.zremrangebyrank(DOMAIN_URLS_KEY.format(domain=domain), 0, -max_urls)
                self.redis.zadd(DOMAIN_SCORE_KEY, {domain: domain_score}, gt=True)

        # Remove the lowest scoring domains
        while self.redis.zcard(DOMAIN_SCORE_KEY) > MAX_OTHER_DOMAINS:
            lowest_scoring_domain = self.redis.zpopmin(DOMAIN_SCORE_KEY)
            self.redis.delete(DOMAIN_URLS_KEY.format(domain=lowest_scoring_domain))

    def get_batch(self) -> list[str]:
        top_scoring_domains = set(self.redis.zrange(DOMAIN_SCORE_KEY, 0, 2000, desc=True))
        top_other_domains = top_scoring_domains - DOMAINS.keys()

        domains = (list(CORE_DOMAINS)
                   + random.sample(DOMAINS.keys(), 50)
                   + random.sample(top_other_domains, 100))

        # Pop the highest scoring URL from each domain
        urls = []
        for domain in domains:
            urls.append(self.redis.zpopmax(DOMAIN_URLS_KEY.format(domain=domain)))
            if len(urls) > BATCH_SIZE:
                break

        return urls
