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
MWMBL_API_KEY = os.environ.get("MWMBL_API_KEY", "")
assert MWMBL_API_KEY.strip(), "An environment variable MWMBL_API_KEY must be set to run the crawler"

# Contact information configuration - required for responsible crawling
MWMBL_CONTACT_INFO = os.environ.get("MWMBL_CONTACT_INFO", "CHANGE_ME@example.com")

# Validate that contact info has been set to a real value
if MWMBL_CONTACT_INFO == "CHANGE_ME@example.com":
    raise ValueError(
        "MWMBL_CONTACT_INFO must be set to your email or website URL. "
        "This allows website administrators to contact you if needed. "
        "Example: contact@yourdomain.com or https://your-mwmbl-instance.com"
    )
