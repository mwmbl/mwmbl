from logging import getLogger
from random import Random
from urllib.parse import urlparse

from redis import Redis

from mwmbl.crawler.domains import DomainLinkDatabase, TOP_DOMAINS
from mwmbl.crawler.urls import FoundURL
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer.blacklist import get_blacklist_domains, is_domain_blacklisted
from mwmbl.settings import CORE_DOMAINS


random = Random(1)
logger = getLogger(__name__)


DOMAIN_URLS_KEY = "domain-urls-{domain}"
DOMAIN_SCORE_KEY = "domain-scores"


MAX_URLS_PER_CORE_DOMAIN = 1000
MAX_URLS_PER_TOP_DOMAIN = 100
MAX_URLS_PER_OTHER_DOMAIN = 5
MAX_OTHER_DOMAINS = 10000

MAX_BATCH_URLS_PER_CORE_DOMAIN = 100
MAX_BATCH_URLS_PER_TOP_DOMAIN = 10
MAX_BATCH_URLS_PER_OTHER_DOMAIN = 1

NUM_TOP_DOMAIN_URLS_TO_INCLUDE = 50
NUM_OTHER_URLS_TO_INCLUDE = 100

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
        self.black_listed_domains = get_blacklist_domains()

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

        logger.info(f"Queued {len(found_urls)} URLs, number of domains: {self.redis.zcard(DOMAIN_SCORE_KEY)}")

    def get_batch(self) -> list[str]:
        top_scoring_domains = set(self.redis.zrange(DOMAIN_SCORE_KEY, 0, 2000, desc=True))
        top_other_domains = top_scoring_domains - DOMAINS.keys()

        domains = list(CORE_DOMAINS)

        if len(DOMAINS) > NUM_TOP_DOMAIN_URLS_TO_INCLUDE:
            domains += random.sample(DOMAINS.keys(), NUM_TOP_DOMAIN_URLS_TO_INCLUDE)
        else:
            domains += list(DOMAINS.keys())

        if len(top_other_domains) > NUM_OTHER_URLS_TO_INCLUDE:
            domains += random.sample(top_other_domains, NUM_OTHER_URLS_TO_INCLUDE)
        else:
            domains += list(top_other_domains)

        domains = [domain for domain in domains if not is_domain_blacklisted(domain, self.black_listed_domains)]
        logger.info(f"Getting batch from domains {domains}")

        # Pop the highest scoring URL from each domain
        urls = []
        for domain in domains:
            domain_urls_scores = self.redis.zpopmax(DOMAIN_URLS_KEY.format(domain=domain))

            # Update the domain score if we removed a URL
            new_domain_scores = self.redis.zrangebyscore(
                DOMAIN_URLS_KEY.format(domain=domain), "-inf", "+inf", start=0, num=1, withscores=True)
            if new_domain_scores:
                new_domain_score = new_domain_scores[0][1]
                self.redis.zadd(DOMAIN_SCORE_KEY, {domain: new_domain_score}, gt=True)
            else:
                self.redis.zrem(DOMAIN_SCORE_KEY, domain)

            for url, score in domain_urls_scores:
                urls.append(url)

            if len(urls) >= BATCH_SIZE:
                break

        logger.info(f"Returning URLs: {urls}")
        return urls
