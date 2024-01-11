"""
Database storing info on URLs
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from logging import getLogger
from pathlib import Path

from django.conf import settings
from psycopg2.extras import execute_values
from pybloomfilter import BloomFilter

# Client has one hour to crawl a URL that has been assigned to them, or it will be reassigned
from mwmbl.utils import batch

REASSIGN_MIN_HOURS = 5
BATCH_SIZE = 100
MAX_URLS_PER_TOP_DOMAIN = 100
MAX_TOP_DOMAINS = 500
MAX_OTHER_DOMAINS = 50000


logger = getLogger(__name__)


class URLStatus(Enum):
    """
    URL state update is idempotent and can only progress forwards.
    """
    NEW = 0                   # One user has identified this URL
    QUEUED = 5                # The URL has been queued for crawling
    ASSIGNED = 10             # The crawler has given the URL to a user to crawl
    ERROR_TIMEOUT = 20        # Timeout while retrieving
    ERROR_404 = 30            # 404 response
    ERROR_OTHER = 40          # Some other error
    ERROR_ROBOTS_DENIED = 50  # Robots disallow this page
    CRAWLED = 100             # At least one user has crawled the URL


CRAWLED_STATUSES = {URLStatus.CRAWLED, URLStatus.ERROR_TIMEOUT, URLStatus.ERROR_404, URLStatus.ERROR_OTHER, URLStatus.ERROR_ROBOTS_DENIED}


@dataclass
class FoundURL:
    url: str
    user_id_hash: str
    score: float
    status: URLStatus
    timestamp: datetime


class URLDatabase:
    def __init__(self):
        self.urls = None

    def __enter__(self):
        try:
            self.urls = BloomFilter.open(settings.URLS_BLOOM_FILTER_PATH)
        except FileNotFoundError:
            logger.info("No existing bloom filter found, creating a new one")
            self.urls = BloomFilter(settings.NUM_URLS_IN_BLOOM_FILTER, 1e-6, settings.URLS_BLOOM_FILTER_PATH, perm=0o666)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.urls.close()

    def update_found_urls(self, found_urls: list[FoundURL]):
        """
        Update URL that have been crawled, and return any that have not yet been crawled
        """
        new_urls = []
        num_crawled_urls = 0
        for url in found_urls:
            if url.url not in self.urls:
                if url.status in CRAWLED_STATUSES:
                    self.urls.add(url.url)
                    num_crawled_urls += 1
                else:
                    new_urls.append(url)

        logger.info(f"Found {num_crawled_urls} crawled URLs and {len(new_urls)} new URLs")
        return new_urls
