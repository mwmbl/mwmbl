"""
Database storing info on URLs
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from logging import getLogger
from pathlib import Path
from typing import Optional

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
    status: URLStatus
    timestamp: datetime
    last_crawled: Optional[datetime] = None


class URLDatabase:
    def __init__(self):
        self.urls = {}

    def __enter__(self):
        month_date = datetime.utcnow()
        for i in range(3):
            # Start from current month and go back two months
            month_date = datetime(month_date.year, month_date.month, 1)
            urls_path = settings.URLS_BLOOM_FILTER_PATH.format(month=month_date.month, year=month_date.year)
            try:
                self.urls[month_date] = BloomFilter.open(urls_path)
            except FileNotFoundError:
                if i == 0:
                    logger.info("No existing bloom filter found, creating a new one")
                    self.urls[month_date] = BloomFilter(settings.NUM_URLS_IN_BLOOM_FILTER, 1e-6, urls_path, perm=0o666)
                else:
                    logger.info("No existing bloom filter found, using fallback")
            month_date -= timedelta(days=1)

        self.urls[datetime(2024, 1, 1)] = BloomFilter.open(settings.URLS_BLOOM_FILTER_FALLBACK_PATH)
        logger.info(f"Initialised URL crawled DB with dates {self.urls.keys()}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for month_date, bloom_filter in self.urls.items():
            bloom_filter.close()

    def update_found_urls(self, found_urls: list[FoundURL]):
        """
        Update URLs with the most recent crawl date, if they've been crawled before, or None otherwise.
        Update the most recent URL status in the database.
        """
        most_recent_urls = next(iter(self.urls.values()))
        new_urls = []
        for url in found_urls:
            found_date = None
            for date, bloom_filter in self.urls.items():
                if url.url in bloom_filter:
                    found_date = date
                    break

            if url.status in CRAWLED_STATUSES:
                most_recent_urls.add(url.url)

            new_urls.append(FoundURL(url.url, url.user_id_hash, url.status, url.timestamp, found_date))
        return new_urls

    def __contains__(self, url):
        for bloom_filter in self.urls.values():
            if url in bloom_filter:
                return True
        return False
