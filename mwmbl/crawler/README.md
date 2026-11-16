# Mwmbl Crawler

This module handles the distributed crawling functionality for Mwmbl. The crawler allows community members to crawl web pages and submit the results to build Mwmbl's search index.

## Environment Variables

The crawler can be configured using the following environment variables:

### Required Configuration
- `MWMBL_API_KEY` - **Required** API key for submitting results to the Mwmbl search index
- `MWMBL_CONTACT_INFO` - **Required** Contact information for website administrators (email or website URL). This is included in the user-agent string so website administrators can contact you if there are issues with the crawler. Example: `contact@yourdomain.com` or `https://your-mwmbl-instance.com`

### Worker Configuration
- `CRAWLER_WORKERS` (default: 2) - Number of separate operating system processes to spawn for crawling work (uses Python's multiprocessing.Process)
- `CRAWL_THREADS` (default: 20) - Number of threads per worker process for concurrent URL crawling (total threads = CRAWLER_WORKERS × CRAWL_THREADS)

**Process Architecture**: The crawler spawns `CRAWLER_WORKERS` processes for crawling work, each using `CRAWL_THREADS` threads for concurrent URL fetching. Additionally, one separate process handles indexing work. For example, with default settings you get 2 crawler processes × 20 threads = 40 total crawling threads, plus 1 indexing process. The queue gives each batch at most one URL per domain, so the threads of one batch never fetch pages from the same site at once.

**Memory**: Memory use scales with `CRAWLER_WORKERS`, not `CRAWL_THREADS`, so on a small machine prefer fewer workers with more threads. The blocklists (about 1.3 million domains) are downloaded once when the crawler starts, and again every 6 hours, by a short-lived process that publishes them to Redis as an ~11MB hash array that every worker loads. Because that first download has to succeed before any worker starts, the crawler exits at startup if it cannot reach the lists. If a later refresh fails, the crawler keeps the lists it has.

Batches crawled by different workers at the same time can share domains, since each worker samples its domains independently. The core domains (Wikipedia, GitHub and so on) are in every batch, so they are the usual overlap. With the default 2 workers, about 0.7% of fetches start while another fetch to the same domain is in flight. Overlap grows with `CRAWLER_WORKERS`. The crawler records it in Redis, and `redis-cli HGETALL crawl-domain-overlap` gives the totals: `fetches`, `overlapped` (fetches that started while another fetch to their domain was in flight) and `duplicate_domains` (should stay 0).

### Benchmarking `CRAWLER_WORKERS` and `CRAWL_THREADS`

`mwmbl benchmark` (or `python -m mwmbl.crawler.benchmark`) finds the best combination for a machine by trying every one, one at a time, and comparing the index score each earns:

```sh
export MWMBL_API_KEY=...   # the crawl key your crawler already uses; no login or password needed
mwmbl benchmark --processes 1,2,3,4,5 --threads 10,25,50,100 --warmup 5m --duration 30m --output results.csv
```

Those are the defaults, apart from `--output`, which is `benchmark-results-<date>.csv` and is appended to, so several runs or machines can share a file. For each combination it stops the crawler, starts it with the two variables set, waits out the warmup, measures, and stops it again. It ends with a ranked table and the winner, and whether it finishes, fails or is interrupted with Ctrl+C it puts the crawler back as it found it.

- `--runner compose` (default) stops and restarts the `mwmbl-crawler` service of `--compose-file`. Settings are passed in the environment of the `docker compose` commands, so `.env` is never edited. Before stopping your crawler it records the `CRAWLER_WORKERS`, `CRAWL_THREADS` and `MWMBL_DEVICE_NAME` the container runs with, wherever they came from, and starts it again with those at the end. Every `up` uses `--pull never`, so a crawler release landing mid-run cannot change the version being measured (the image must already be present, as it is for a running crawler). The crawler is started with `MWMBL_DEVICE_NAME` set to `--device-name` (else `MWMBL_DEVICE_NAME`, else `~/.mwmbl/device-name`, created if missing), so the benchmark may appear as a new device; pass the name your crawler already uses to avoid that.
- `--runner process` runs `--crawler-command` (default `mwmbl-crawl`) as a child process, and stops its whole process group, workers included, between configurations. It refuses to start while another crawler is running, so stop yours first.

The benchmark reads `MWMBL_API_KEY` and `MWMBL_REMOTE_SERVER` from its own environment, and the crawler takes its own from compose or `.env`: they must be the same server and key, and `CRAWL_SUBMIT_MODE` must be `index`. If the server sees nothing from the device in a measurement window, the benchmark stops after that first configuration rather than failing every one of them in turn, and says what to check.

**What the score is.** For every submission the server adds the number of URLs that were new to the index to the device's totals, and keeps each submission for 30 days. The benchmark asks `GET /api/v1/crawler/device-results?device_name=...&since=...`, authenticated with the same crawl-scoped `MWMBL_API_KEY` as the crawler, which covers only that key's owner's devices. At the end of warmup it takes the server's clock from a first call, and at the end of the measurement it asks for the results submitted since then. The score is therefore what the server counted from the device in that window, timed by the server's clock, not a difference of two totals or the devices page's rolling "last 24 hours", which every configuration shares. A submission is timed from when its request arrived, so one still being indexed at a boundary is credited to the earlier side. Local Redis holds only the crawl queue, and the crawler is never told how many of its results were new, so the server's count is the only exact measure.

**What it cannot tell you.** Each combination runs once, so differences of a few percent are noise. Results reach the server in batches some time after crawling, and stopping the crawler leaves queued batches in its Redis for the next configuration to submit: the warmup exists to absorb that, so do not set it to nothing. A short `--duration` gives a rougher number. Combinations run in a fixed order, and the pages a crawler finds new fall as the index fills. Repeat a close contest, in the opposite order if you can.

**What it optimises, and what it should not.** The score counts new URLs, which says nothing about their quality, and more threads always fetch more. The sweep reaches 500 threads, and the crawler's politeness limits (`mwmbl/crawler/politeness.py`) are kept per Redis, so they hold for one crawler's workers but not across volunteers' machines. Treat the winner as the most your machine can usefully do, not a target to push past what the sites you crawl, or your connection, should bear: look at the refusals in the crawler's logs, and prefer the cheaper configuration when two are close. Ties are broken that way in the report.

Combinations whose estimated memory (`--memory-per-process-mb` per process plus a fixed 600MB) exceeds the machine's RAM are marked `SKIPPED`; `--no-memory-check` runs them anyway. The winner is always chosen by score.

The CSV has the timestamp, `hostname`, crawler version as the server last recorded it, process and thread counts, warmup and measurement seconds, index score, status and any error. `hostname` is the device name (Mwmbl's own term for it), not this machine's hostname, which the crawler deliberately never reports and the CSV does not record, so the file can be shared.

### Rate Limiting Configuration
- `CRAWL_DELAY_SECONDS` (default: 0.0) - Delay in seconds each crawl thread waits between the URLs it crawls. Includes 10% random fuzz (±10%) to avoid synchronized requests across workers. Set to 0 to disable delays.

Independently of that setting, the crawler limits how often it fetches from any one domain (`mwmbl/crawler/politeness.py`). A batch claims each domain it takes a URL from, so no other worker sharing the Redis can fetch from that domain at the same time. After the batch, the domain stays unavailable for the `Crawl-delay` its robots.txt asks for, or at least 5 seconds. A domain that answers 403, 429, 502, 503 or 504 is left alone for a minute, doubling with each refusal up to six hours, until a fetch gets through. Its URLs stay queued in the meantime.

If robots.txt cannot be fetched (a 5xx, a timeout, a refused connection) the site is treated as disallowing everything for an hour, as RFC 9309 asks. A 4xx means there is no robots.txt and everything is allowed. Pages that come back with a status outside 2xx are not indexed and their links are not followed, so a Cloudflare challenge or an error page never reaches the index.

### Error Reporting
- `CRAWL_REPORT_ERRORS` (default: true) - Send the errors the crawler catches and recovers from to the Mwmbl server (`POST /api/v1/crawler/errors`), so the maintainers can see when crawlers are failing. A report holds the exception, its traceback, the crawler version and the device name. File paths are sent relative to the Python path and your home directory is replaced with `~`, so reports do not name your account. Each distinct error is reported at most once an hour per process. Set to `false` to opt out. Nothing is reported when `CRAWL_SUBMIT_MODE` is `off`.

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
2. **Batch Processing**: The system assigns up to 100 URLs at a time (configurable via the `CRAWL_BATCH_SIZE` environment variable)
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
2. **Concurrent Crawling**: Crawls the batch on `CRAWL_THREADS` threads, at most one URL per domain, skipping domains fetched too recently
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
