import json
import math
from datetime import datetime, timedelta

from logging import getLogger
from random import Random
from typing import Callable
from urllib.parse import urlparse

from redis import Redis

from mwmbl.crawler.domains import DomainLinkDatabase, TOP_DOMAINS
from mwmbl.crawler.urls import FoundURL
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer.blacklist import get_blacklist_domains, is_domain_blacklisted
from mwmbl.settings import CORE_DOMAINS


MAX_TIME_DELTA = timedelta(days=100000)

random = Random(1)
logger = getLogger(__name__)


DOMAIN_URLS_KEY = "domain-urls-{domain}"
DOMAIN_SCORE_KEY = "domain-scores"
USER_URLS_KEY = "user-urls-{user_id}"

USER_EXPIRY_SECONDS = 60 * 60 * 24 * 7


MAX_URLS_PER_CORE_DOMAIN = 1000
MAX_URLS_PER_TOP_DOMAIN = 100
MAX_URLS_PER_OTHER_DOMAIN = 5
MAX_OTHER_DOMAINS = 10000

NUM_TOP_DOMAIN_URLS_TO_INCLUDE = 50
NUM_OTHER_URLS_TO_INCLUDE = 100

BATCH_SIZE = 100

# Discount URLs crawled recently - this is the scale - currently 10 months
SCORE_TIME_CONSTANT = 60 * 60 * 24 * 30 * 10


def get_domain_max_urls(domain: str, curated_domains: set[str]):
    if domain in CORE_DOMAINS | curated_domains:
        return MAX_URLS_PER_CORE_DOMAIN
    elif domain in TOP_DOMAINS:
        return MAX_URLS_PER_TOP_DOMAIN
    else:
        return MAX_URLS_PER_OTHER_DOMAIN


class RedisURLQueue:
    def __init__(self, redis: Redis, get_curated_domains_function: Callable[[], set[str]]) -> None:
        self.redis = redis
        self.black_listed_domains = get_blacklist_domains()
        self.get_curated_domains_function = get_curated_domains_function

    def queue_urls(self, found_urls: list[FoundURL]):
        curated_domains = self.get_curated_domains_function()
        logger.info(f"Got {len(found_urls)} URLs, {len(curated_domains)} curated domains")
        with DomainLinkDatabase() as link_db:
            for url in found_urls:
                time_since_crawled = (datetime.utcnow() - url.last_crawled
                                      if url.last_crawled is not None else MAX_TIME_DELTA)

                # Skip URLs crawled in the last month
                if time_since_crawled < timedelta(days=30):
                    continue

                domain = urlparse(url.url).netloc
                url_score = 1/len(url.url)

                # Discount URLs that were crawled recently
                score_multiplier = 1 - math.exp(-time_since_crawled.total_seconds() / SCORE_TIME_CONSTANT)
                url_score *= score_multiplier
                logger.info(f"URL score: {url_score}, score multiplier: {score_multiplier} for domain {domain} and age {time_since_crawled}")

                domain_score = link_db.get_domain_score(domain) + url_score
                max_urls = get_domain_max_urls(domain, curated_domains)
                self.redis.zadd(DOMAIN_URLS_KEY.format(domain=domain), {url.url: url_score})
                self.redis.zremrangebyrank(DOMAIN_URLS_KEY.format(domain=domain), 0, -(max_urls + 1))
                self.redis.zadd(DOMAIN_SCORE_KEY, {domain: domain_score}, gt=True)

        # Remove the lowest scoring domains
        while self.redis.zcard(DOMAIN_SCORE_KEY) > MAX_OTHER_DOMAINS:
            lowest_scoring_domain = self.redis.zpopmin(DOMAIN_SCORE_KEY)
            self.redis.delete(DOMAIN_URLS_KEY.format(domain=lowest_scoring_domain))

        logger.info(f"Queued {len(found_urls)} URLs, number of domains: {self.redis.zcard(DOMAIN_SCORE_KEY)}")

    def get_batch(self, user_id: str) -> list[str]:
        top_scoring_domains = set(self.redis.zrange(DOMAIN_SCORE_KEY, 0, 2000, desc=True))
        top_other_domains = top_scoring_domains - DOMAINS.keys()
        curated_domains = self.get_curated_domains_function()

        domains = list(CORE_DOMAINS)
        top_curated_domains = (DOMAINS.keys() & top_scoring_domains) | curated_domains
        if len(top_curated_domains) > NUM_TOP_DOMAIN_URLS_TO_INCLUDE:
            domains += random.sample(top_curated_domains, NUM_TOP_DOMAIN_URLS_TO_INCLUDE)
        else:
            domains += list(top_curated_domains)

        if len(top_other_domains) > NUM_OTHER_URLS_TO_INCLUDE:
            domains += random.sample(top_other_domains, NUM_OTHER_URLS_TO_INCLUDE)
        else:
            domains += list(top_other_domains)

        domains = [domain for domain in domains if not is_domain_blacklisted(domain, self.black_listed_domains)]
        logger.info(f"Getting batch from domains {domains}")

        # Add a random url as the root domain of one of DOMAINS
        random_domain = random.choice(list(DOMAINS.keys() | curated_domains))
        urls = [f"https://{random_domain}/"]

        # Pop the highest scoring URL from each domain
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

        # Assign the URLs to this user
        user_url_str = json.dumps(urls)
        self.redis.set(USER_URLS_KEY.format(user_id=user_id), user_url_str)
        self.redis.expire(USER_URLS_KEY.format(user_id=user_id), USER_EXPIRY_SECONDS)

        return urls

    def check_user_crawled_urls(self, user_id: str, urls: list[str]):
        user_assigned_urls = self.redis.get(USER_URLS_KEY.format(user_id=user_id))
        if user_assigned_urls is None:
            return urls

        user_assigned_url_set = set(json.loads(user_assigned_urls))
        return [url for url in urls if url not in user_assigned_url_set]

    def get_domain_count(self, domain: str):
        return self.redis.zcard(DOMAIN_URLS_KEY.format(domain=domain))
