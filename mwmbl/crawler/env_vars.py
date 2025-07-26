"""
Environment variables configuration for the crawler module.
"""

import os


# Crawler worker configuration
CRAWLER_WORKERS = int(os.environ.get("CRAWLER_WORKERS", "2"))
CRAWL_THREADS = int(os.environ.get("CRAWL_THREADS", "20"))

# Rate limiting configuration
CRAWL_DELAY_SECONDS = float(os.environ.get("CRAWL_DELAY_SECONDS", "0.0"))

# API configuration
MWMBL_API_KEY = os.environ.get("MWMBL_API_KEY", "")

# Contact information configuration - required for responsible crawling
MWMBL_CONTACT_INFO = os.environ.get("MWMBL_CONTACT_INFO", "CHANGE_ME@example.com")
