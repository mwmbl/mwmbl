"""
Environment variables configuration for the crawler module.
"""
import os


# Client has one hour to crawl a URL that has been assigned to them, or it will be reassigned
REASSIGN_MIN_HOURS = int(os.environ.get('REASSIGN_MIN_HOURS', '5'))
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '100'))
MAX_URLS_PER_TOP_DOMAIN = int(os.environ.get('MAX_URLS_PER_TOP_DOMAIN', '100'))
MAX_TOP_DOMAINS = int(os.environ.get('MAX_TOP_DOMAINS', '500'))
MAX_OTHER_DOMAINS = int(os.environ.get('MAX_OTHER_DOMAINS', '50000'))

# Retrieval configuration
TIMEOUT_SECONDS = int(os.environ.get('TIMEOUT_SECONDS', '3'))
MAX_FETCH_SIZE = int(os.environ.get('MAX_FETCH_SIZE', str(1024*1024)))
MAX_NEW_LINKS = int(os.environ.get('MAX_NEW_LINKS', '50'))
MAX_EXTRA_LINKS = int(os.environ.get('MAX_EXTRA_LINKS', '50'))
MAX_SITE_URLS = int(os.environ.get('MAX_SITE_URLS', '100'))

# Crawler worker configuration
CRAWLER_WORKERS = int(os.environ.get('CRAWLER_WORKERS', '10'))

# Redis configuration
REDIS_URL = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379')
