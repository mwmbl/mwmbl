"""
Environment variables configuration for the crawler module.
"""

import os


# Crawler worker configuration
CRAWLER_WORKERS = int(os.environ.get("CRAWLER_WORKERS", "10"))
CRAWL_THREADS = int(os.environ.get("CRAWL_THREADS", "20"))

# Rate limiting configuration
CRAWL_DELAY_SECONDS = float(os.environ.get("CRAWL_DELAY_SECONDS", "0.0"))

# Redis configuration
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")

# API configuration
MWMBL_API_KEY = os.environ["MWMBL_API_KEY"]
