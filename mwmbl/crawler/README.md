# Mwmbl Crawler

This module handles the distributed crawling functionality for Mwmbl. The crawler allows community members to crawl web pages and submit the results to build Mwmbl's search index.

## Environment Variables

The crawler can be configured using the following environment variables:

### Crawling Assignment
- `REASSIGN_MIN_HOURS` (default: 5) - Hours before a URL assigned to a client is reassigned if not crawled
- `BATCH_SIZE` (default: 100) - Number of URLs in each crawling batch
- `MAX_URLS_PER_TOP_DOMAIN` (default: 100) - Maximum URLs per top domain (for example wikipedia, reddit, etc)
- `MAX_TOP_DOMAINS` (default: 500) - Maximum number of top domains to process
- `MAX_OTHER_DOMAINS` (default: 50000) - Maximum number of other domains to process

### URL Retrieval Configuration  
- `TIMEOUT_SECONDS` (default: 3) - Timeout for fetching individual URLs
- `MAX_FETCH_SIZE` (default: 1048576) - Maximum size in bytes to fetch per URL (1MB)
- `MAX_NEW_LINKS` (default: 50) - Maximum new links to extract from good content paragraphs
- `MAX_EXTRA_LINKS` (default: 50) - Maximum extra links to extract from non-good paragraphs
- `MAX_SITE_URLS` (default: 100) - TODO: clarify usage of this variable

### Redis Configuration
- `REDIS_URL` (default: 'redis://127.0.0.1:6379') - Redis connection URL for URL queues and stats

## Setup

For development setup and deployment configuration, see `./docker-compose.yml` at the root of the repository.

## How It Works

The crawler is part of Mwmbl's distributed approach where community members contribute to building the search index by crawling web pages. See the main [README](../../README.md) for more context on Mwmbl's community-driven approach.
