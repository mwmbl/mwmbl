import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from logging import getLogger
from random import Random
from typing import Callable

from redis import Redis

from mwmbl.crawler.domains import TOP_DOMAINS, DomainLinkDatabase
from mwmbl.crawler.env_vars import CRAWL_ALLOWED_DOMAINS, CRAWL_BATCH_SIZE
from mwmbl.crawler.urls import FoundURL
from mwmbl.hn_top_domains_filtered import DOMAINS
from mwmbl.indexer.blacklist import get_default_blacklist_provider
from mwmbl.settings import CORE_DOMAINS
from mwmbl.utils import parse_url

MAX_TIME_DELTA = timedelta(days=100000)

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
# Promoted domains go into a batch ahead of everything else, best first.
NUM_PROMOTED_DOMAINS_TO_INCLUDE = 50

# Seeded so that two CI runs with the same allowlist start from the same place. Only the
# allowlist path uses it: forked crawl workers each get a copy, so they would seed in lockstep.
ALLOWLIST_RANDOM = Random(1)


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
    def __init__(
        self,
        redis: Redis,
        get_curated_domains_function: Callable[[], set[str]],
        blacklist_provider=None,
        get_promoted_domains_function: Callable[[], list[str]] = lambda: [],
    ) -> None:
        """
        get_promoted_domains_function returns the promoted domains best first: the seed
        domains the server scores as most likely to add new pages that answer searches. They
        take priority over curated domains when a batch is put together.
        """
        self.redis = redis
        self.get_curated_domains_function = get_curated_domains_function
        self.get_promoted_domains_function = get_promoted_domains_function
        # Curated domains override the blacklist, and the queue already knows where to get
        # them - over HTTP in the standalone crawler, which has no database.
        self.blacklist_provider = blacklist_provider or get_default_blacklist_provider(get_curated_domains_function)

    def queue_urls(self, found_urls: list[FoundURL]):
        curated_domains = self.get_curated_domains_function()
        promoted_domains = self.get_promoted_domains_function()
        logger.info(
            f"Got {len(found_urls)} URLs, {len(curated_domains)} curated domains, "
            f"{len(promoted_domains)} promoted domains"
        )
        # A promoted domain is only worth crawling for its depth, so it can queue as many URLs
        # as a curated one.
        deep_domains = curated_domains | set(promoted_domains)
        url_scores = defaultdict(list)
        domain_scores = {}
        with DomainLinkDatabase() as link_db:
            for url in found_urls:
                time_since_crawled = (
                    datetime.now(timezone.utc) - url.last_crawled if url.last_crawled is not None else MAX_TIME_DELTA
                )

                # Skip URLs crawled in the last month
                if time_since_crawled < timedelta(days=30):
                    continue

                domain = parse_url(url.url).netloc
                url_score = 1 / len(url.url)

                # Discount URLs that were crawled recently
                score_multiplier = 1 - math.exp(-time_since_crawled.total_seconds() / SCORE_TIME_CONSTANT)
                url_score *= score_multiplier
                logger.info(
                    f"URL score: {url_score}, score multiplier: {score_multiplier} for domain {domain} and age {time_since_crawled}"
                )

                url_scores[domain].append((url.url, url_score))
                domain_score = link_db.get_domain_score(domain) + url_score
                domain_scores[domain] = max(domain_score, domain_scores.get(domain, 0.0))

        if len(domain_scores) > 0:
            self.redis.zadd(DOMAIN_SCORE_KEY, domain_scores, gt=True)

        for domain in domain_scores.keys():
            self.redis.zadd(DOMAIN_URLS_KEY.format(domain=domain), dict(url_scores[domain]))
            max_urls = get_domain_max_urls(domain, deep_domains)
            self.redis.zremrangebyrank(DOMAIN_URLS_KEY.format(domain=domain), 0, -(max_urls + 1))

        # Remove the lowest scoring domains
        while self.redis.zcard(DOMAIN_SCORE_KEY) > MAX_OTHER_DOMAINS:
            lowest_scoring_domain = self.redis.zpopmin(DOMAIN_SCORE_KEY)
            self.redis.delete(DOMAIN_URLS_KEY.format(domain=lowest_scoring_domain))

        logger.info(f"Queued {len(found_urls)} URLs, number of domains: {self.redis.zcard(DOMAIN_SCORE_KEY)}")

    def get_batch(self, user_id: str) -> list[str]:
        curated_domains = self.get_curated_domains_function()

        if CRAWL_ALLOWED_DOMAINS:
            # An allowlist replaces the candidates outright: a CI crawl has to touch a set
            # of sites we chose, not a sample of whatever the live queue happens to hold.
            # Sorting keeps the seed choice reproducible despite the randomised
            # PYTHONHASHSEED the crawler image sets.
            domains = sorted(CRAWL_ALLOWED_DOMAINS)
            seed_domains = sorted(CRAWL_ALLOWED_DOMAINS)
            seed_random = ALLOWLIST_RANDOM
        else:
            promoted_domains = [
                domain
                for domain in self.get_promoted_domains_function()
                if not self.blacklist_provider.is_domain_blacklisted(domain)
            ]
            promoted_domain_scores = self.redis.zmscore(DOMAIN_SCORE_KEY, promoted_domains) if promoted_domains else []
            queued_promoted_domains = [
                domain for domain, score in zip(promoted_domains, promoted_domain_scores) if score is not None
            ]
            # A promoted domain this crawler has never seen has nothing queued, so the only way
            # in is to seed its root page, whose links then fill its queue.
            unqueued_promoted_domains = [
                domain for domain, score in zip(promoted_domains, promoted_domain_scores) if score is None
            ]

            top_scoring_domains = set(self.redis.zrange(DOMAIN_SCORE_KEY, 0, 2000, desc=True))
            top_other_domains = top_scoring_domains - DOMAINS.keys()

            # The stdlib's shared random instance, which Python reseeds in every forked child. A
            # Random of our own, seeded or not, would be copied into each crawl worker, and every
            # worker would then pick the same domains and seed URL in lockstep.
            domains = queued_promoted_domains[:NUM_PROMOTED_DOMAINS_TO_INCLUDE] + list(CORE_DOMAINS)
            top_curated_domains = (DOMAINS.keys() & top_scoring_domains) | curated_domains
            if len(top_curated_domains) > NUM_TOP_DOMAIN_URLS_TO_INCLUDE:
                domains += random.sample(list(top_curated_domains), NUM_TOP_DOMAIN_URLS_TO_INCLUDE)
            else:
                domains += list(top_curated_domains)

            if len(top_other_domains) > NUM_OTHER_URLS_TO_INCLUDE:
                domains += random.sample(list(top_other_domains), NUM_OTHER_URLS_TO_INCLUDE)
            else:
                domains += list(top_other_domains)

            seed_domains = unqueued_promoted_domains or list(DOMAINS.keys() | curated_domains)
            seed_random = random

        # Add a random url as the root domain of one of DOMAINS. The seed needs the same
        # blacklist filter as the rest: it is a URL we are about to fetch.
        seed_domains = [domain for domain in seed_domains if not self.blacklist_provider.is_domain_blacklisted(domain)]
        random_domain = seed_random.choice(seed_domains)
        urls = [f"https://{random_domain}/"]

        # At most one URL per domain, since the batch is crawled concurrently: the samples
        # above can pick a core domain again, and the seed can repeat any of them. Unless the
        # seed's domain is the only one, as with a single-domain allowlist: dropping it then
        # would leave that domain's queued URLs never popped.
        domains = [
            domain for domain in dict.fromkeys(domains) if not self.blacklist_provider.is_domain_blacklisted(domain)
        ]
        if domains != [random_domain]:
            domains = [domain for domain in domains if domain != random_domain]
        logger.info(f"Getting batch from domains {domains}")

        # Pop the highest scoring URL from each domain
        for domain in domains:
            domain_urls_scores = self.redis.zpopmax(DOMAIN_URLS_KEY.format(domain=domain))

            # Update the domain score if we removed a URL
            new_domain_scores = self.redis.zrangebyscore(
                DOMAIN_URLS_KEY.format(domain=domain), "-inf", "+inf", start=0, num=1, withscores=True
            )
            if new_domain_scores:
                new_domain_score = new_domain_scores[0][1]
                self.redis.zadd(DOMAIN_SCORE_KEY, {domain: new_domain_score}, gt=True)
            else:
                self.redis.zrem(DOMAIN_SCORE_KEY, domain)

            for url, score in domain_urls_scores:
                urls.append(url)

            if len(urls) >= CRAWL_BATCH_SIZE:
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
