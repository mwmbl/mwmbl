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

# Bounds for running the crawler somewhere that is not a volunteer's machine - CI, mainly.
# The defaults are the unbounded behaviour a volunteer crawler has always had.

# URLs assigned per batch.
CRAWL_BATCH_SIZE = int(os.environ.get("CRAWL_BATCH_SIZE", "100"))

# Comma-separated domains to crawl. When set, these replace the queue's usual candidates,
# so a run touches only sites we chose rather than a sample of the live URL queue.
CRAWL_ALLOWED_DOMAINS = frozenset(
    domain.strip() for domain in os.environ.get("CRAWL_ALLOWED_DOMAINS", "").split(",") if domain.strip()
)

# What to do with results the crawler decides are worth submitting:
#   index    - post them normally, so they go into the Mwmbl index (the default)
#   dry-run  - post them with ?dry_run=true, so the server authenticates and validates
#              them but indexes nothing
#   off      - do not post at all, and do not require an API key
SUBMIT_MODE_INDEX = "index"
SUBMIT_MODE_DRY_RUN = "dry-run"
SUBMIT_MODE_OFF = "off"
SUBMIT_MODES = {SUBMIT_MODE_INDEX, SUBMIT_MODE_DRY_RUN, SUBMIT_MODE_OFF}

CRAWL_SUBMIT_MODE = os.environ.get("CRAWL_SUBMIT_MODE", SUBMIT_MODE_INDEX)
if CRAWL_SUBMIT_MODE not in SUBMIT_MODES:
    raise ValueError(f"CRAWL_SUBMIT_MODE must be one of {sorted(SUBMIT_MODES)}, got {CRAWL_SUBMIT_MODE!r}")
