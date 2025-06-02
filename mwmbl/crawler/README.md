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
- `CRAWLER_WORKERS` (default: 10) - Number of separate operating system processes to spawn for crawling work (uses Python's multiprocessing.Process)
- `CRAWL_THREADS` (default: 20) - Number of threads per worker process for concurrent URL crawling (total threads = CRAWLER_WORKERS × CRAWL_THREADS)
- `CRAWLER_LOG_LEVEL` (default: INFO) - Log level for crawler output. Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL

**Process Architecture**: The crawler spawns `CRAWLER_WORKERS` processes for crawling work, each using `CRAWL_THREADS` threads for concurrent URL fetching. Additionally, one separate process handles indexing work. For example, with default settings you get 10 crawler processes × 20 threads = 200 total crawling threads, plus 1 indexing process.

### Rate Limiting Configuration
- `CRAWL_DELAY_SECONDS` (default: 0.0) - Delay in seconds between crawling each URL within a batch. Includes 10% random fuzz (±10%) to avoid synchronized requests across workers. Set to 0 to disable delays.

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
5. **Domain Authority Scoring**: Tracks which domains link to which other domains using `DOMAIN_GROUPS` - a predefined list of high-authority domains and their scoring weights (e.g., GitHub, Wikipedia, HackerNews get weight 10; Lemmy/Mastodon get weight 2; top domains get weight 5; others get weight 1). This creates a domain authority system where links from high-authority sources boost the ranking of target pages.
6. **Statistics Tracking**: Records detailed crawl statistics per domain and user in Redis

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

### Batch Processing Workflow

The **batch processing** handles the actual web crawling work:

1. **URL Assignment**: Gets batches of URLs from the Redis URL queue 
2. **Sequential Crawling**: Crawls each URL one-by-one with configurable delays between requests (respects rate limits)
3. **Content Extraction**: Uses justext library to extract clean text content and discover new links
4. **Result Recording**: Records crawl results in database for URL tracking and deduplication
5. **Queue Handoff**: Pushes completed batches to Redis queue for the indexing process

Each batch is processed as a `HashedBatch` object containing metadata (user ID, timestamp) and an array of crawl results (URL, title, extract, links, errors).

### Indexing Workflow  

The **indexing process** transforms crawl results into searchable content:

1. **Batch Retrieval**: Pulls completed crawl batches from Redis queue (processes up to 10 batches at once)
2. **Local Indexing**: Uses the tiny search engine indexer to build local search index from crawl results
3. **Quality Filtering**: For high-activity search terms, compares local results against the remote Mwmbl index
4. **Selective Sync**: Only submits local results that score higher than existing remote results (prevents low-quality content pollution)
5. **Remote Integration**: Downloads updated remote results and merges them back into local index

This two-stage process ensures that:
- High-quality crawl results reach the main Mwmbl search index
- Local crawlers maintain up-to-date search indexes
- The main index maintains quality standards through score-based filtering

### Community-Driven Approach

The distributed nature allows the crawler to:
- Scale horizontally with more community contributors
- Respect rate limits across many different IP addresses
- Reduce load on any single crawler instance
- Build a diverse, community-curated web index

See the main [README](../../README.md) for more context on Mwmbl's community-driven approach.
