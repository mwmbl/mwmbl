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

### Worker Configuration
- `CRAWLER_WORKERS` (default: 10) - Number of parallel crawler worker processes to run
- `CRAWL_THREADS` (default: 20) - Number of threads per worker process for concurrent URL crawling

### Redis Configuration
- `REDIS_URL` (default: 'redis://127.0.0.1:6379') - Redis connection URL for URL queues and stats

## Setup

For development setup and deployment configuration, see `./docker-compose.yml` at the root of the repository.

## Architecture Overview

The Mwmbl crawler is a **distributed, collaborative web crawler** where multiple users help crawl the web to build Mwmbl's search index. The system coordinates URL assignment, deduplication, and result aggregation.

### What Data It Fetches

The crawler fetches **web pages (HTML content)** from URLs, specifically:
- Page titles and main text content (using the justext library for clean content extraction)
- Internal and external links found on each page
- HTTP status codes and error information
- Respects robots.txt files before crawling any URL

### How It Works (Request/Response Flow)

This is an **on-demand, distributed crawler**:

1. **URL Assignment**: Users request batches of URLs to crawl via `/api/v1/crawler/batches/new`
2. **Batch Processing**: The system assigns up to 100 URLs at a time (configurable via `BATCH_SIZE`)
3. **Crawling**: Users crawl those URLs with rate limiting (configurable delay between requests)
4. **Result Submission**: Users submit crawled results back via `/api/v1/crawler/batches/`

### Data Processing Pipeline

The crawler processes data through several stages:

1. **Content Extraction**: Uses the justext library to extract clean, readable text content from HTML
2. **Link Discovery**: Finds and validates new links (up to 50 content links + 50 navigation links per page)
3. **Data Validation**: Filters out problematic URLs (localhost, binary files like .jpg/.pdf, etc.)
4. **Deduplication**: Uses bloom filters to track which URLs have already been crawled
5. **Statistics Tracking**: Records detailed crawl statistics per domain and user in Redis

### What Data Gets Uploaded

The crawler uploads several types of data:

#### 1. Crawled Batches (to S3-compatible storage)
Compressed JSON files containing:
- URL, title, and extracted text content
- Discovered links categorized by type (content vs navigation)
- Crawl timestamps and HTTP status codes
- Any errors encountered during crawling

#### 2. Search Results (for indexing)
Processed data for the tiny search engine:
- URL, title, extract text
- Used to build the searchable index that powers Mwmbl search

#### 3. Statistics (to Redis)
Real-time monitoring data:
- URLs crawled per day/hour
- Top contributing users and domains
- Domain link relationships for scoring
- Error rates and success metrics

### Community-Driven Approach

The distributed nature allows the crawler to:
- Scale horizontally with more community contributors
- Respect rate limits across many different IP addresses
- Reduce load on any single crawler instance
- Build a diverse, community-curated web index

See the main [README](../../README.md) for more context on Mwmbl's community-driven approach.
