import logging
import os
import random
import time
from datetime import datetime, timedelta
from multiprocessing import Process
from pathlib import Path

import django
import requests
from django.conf import settings
from redis import Redis

from mwmbl.crawler.env_vars import CRAWLER_WORKERS, CRAWL_DELAY_SECONDS, MWMBL_API_KEY, MWMBL_CONTACT_INFO
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import score_result
from mwmbl.tokenizer import tokenize

os.environ["DJANGO_SETTINGS_MODULE"] = "mwmbl.settings_crawler"

data_path = Path(settings.DATA_PATH)
print("Data path", data_path)
data_path.mkdir(exist_ok=True, parents=True)

print("Mwmbl crawling statistics: https://mwmbl.org/stats")

django.setup()

from mwmbl.indexer.update_urls import record_urls_in_database
from mwmbl.crawler.retrieve import crawl_batch, crawl_url, CRAWLER_VERSION
from mwmbl.crawler.batch import HashedBatch, Result, Results
from mwmbl.indexer.index_batches import index_batches, index_pages

logger = logging.getLogger(__name__)
FORMAT = "%(process)d:%(levelname)s:%(name)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

BATCH_QUEUE_KEY = "batch-queue"
REMOTE_SERVER = "https://api.mwmbl.org"


# Validate environment variables when actually needed
def validate_environment():
    """Validate required environment variables for crawler operation."""
    if not MWMBL_API_KEY.strip():
        raise ValueError("An environment variable MWMBL_API_KEY must be set to run the crawler")
    
    if MWMBL_CONTACT_INFO == "CHANGE_ME@example.com":
        raise ValueError(
            "MWMBL_CONTACT_INFO must be set to your email or website URL. "
            "This allows website administrators to contact you if needed. "
            "Example: contact@yourdomain.com or https://your-mwmbl-instance.com"
        )


class Crawler:
    """
    Main crawler class that manages Redis connections, URL queues, and crawling operations.
    
    This class encapsulates all the crawler functionality including:
    - Redis connection management
    - URL queue operations
    - Batch processing
    - Indexing operations
    """
    
    def __init__(self, redis_url=None):
        """
        Initialize the crawler with Redis connection.
        
        Args:
            redis_url: Optional Redis URL. If not provided, uses settings.REDIS_URL
        """
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis = None
        self._url_queue = None
    
    @property
    def redis(self):
        """Lazy initialization of Redis connection."""
        if self._redis is None:
            self._redis = Redis.from_url(
                self.redis_url,
                decode_responses=True,
                health_check_interval=30,
            )
        return self._redis
    
    @property
    def url_queue(self):
        """Lazy initialization of URL queue."""
        if self._url_queue is None:
            self._url_queue = RedisURLQueue(self.redis, lambda: set())
        return self._url_queue
    
    def check_redis(self):
        """Check Redis connection health."""
        try:
            self.redis.ping()
            logger.debug("Redis ping successful")
        except ConnectionError as e:
            raise SystemExit(f"Cannot reach Redis at {self.redis_url}. Make sure your Redis server is running.")
    
    def process_batch(self):
        """
        Process a single batch of URLs by crawling them sequentially with rate limiting.

        This function handles the core crawling workflow:
        1. Gets a batch of URLs from the Redis URL queue
        2. Crawls each URL sequentially with configurable delay between requests
        3. Records crawl results in the database for URL tracking
        4. Pushes the completed batch to Redis queue for indexing

        The sequential crawling with delays respects rate limits and reduces load on target servers.
        Each batch is processed as a HashedBatch object containing metadata and crawl results.
        """
        user_id = "test"
        urls = self.url_queue.get_batch(user_id)
        logger.info(f"Processing batch of {len(urls)} URLs")

        # Process URLs sequentially with rate limiting
        results = []
        for i, url in enumerate(urls):
            if i > 0:  # Don't delay before the first URL
                # Add delay with 10% random fuzz
                delay = CRAWL_DELAY_SECONDS * (0.9 + 0.2 * random.random())
                time.sleep(delay)

            result = crawl_url(url)
            results.append(result)
            logger.debug("Result", result)
        
        js_timestamp = int(time.time() * 1000)
        batch = HashedBatch.parse_obj({
            "user_id_hash": user_id, 
            "timestamp": js_timestamp, 
            "items": results,
        })
        record_urls_in_database([batch], self.url_queue)

        # Push the batch into the Redis queue
        batch_json = batch.json()
        self.redis.rpush(BATCH_QUEUE_KEY, batch_json)
    
    def run_indexing(self):
        """
        Process completed crawl batches and integrate results into the search index.

        This function handles the indexing workflow:
        1. Pulls completed crawl batches from Redis queue (up to 10 at once)
        2. Indexes batches locally using the tiny search engine indexer
        3. For top terms, syncs high-scoring local results with the remote Mwmbl index
        4. Downloads updated remote results and merges them back into local index

        The sync process ensures that high-quality local crawl results get submitted
        to the main Mwmbl search index, while also keeping the local index updated
        with the latest remote results for better search quality.

        Only results that score higher than existing remote results are submitted,
        preventing low-quality content from polluting the main index.
        """
        index_path = data_path / settings.INDEX_NAME
        batch_jsons = self.redis.lpop(BATCH_QUEUE_KEY, 10)
        if batch_jsons is None:
            logger.info("No more batches to index. Sleeping for 10 seconds.")
            time.sleep(10)
            return
        
        logger.info(f"Got {len(batch_jsons)} batches to index")
        batches = [HashedBatch.parse_raw(b) for b in batch_jsons]
        term_new_doc_count = index_batches(batches, index_path)
        logger.info(f"Indexed, top terms to sync: {term_new_doc_count.most_common(10)}")

        remote_index = RemoteIndex()
        with TinyIndex(Document, index_path, 'w') as local_index:
            for term, count in term_new_doc_count.most_common(100):
                logger.info(f"Syncing term {term} with {count} new local items")
                remote_items = remote_index.retrieve(term)
                remote_item_urls = {item.url for item in remote_items}
                local_items = local_index.retrieve(term)
                new_items = [item for item in local_items if item.url not in remote_item_urls]
                logger.info(f"Found {len(new_items)} new items for term {term}")

                terms = tokenize(term)
                remote_item_scores = [score_result(terms, item, True) for item in remote_items]
                min_remote_score = min(remote_item_scores, default=0.0)
                local_scores = [score_result(terms, item, True) for item in new_items]
                max_local_score = max(local_scores, default=0.0)
                logger.info(f"Max local score: {max_local_score}, min remote score: {min_remote_score}")

                new_high_score = max_local_score < min_remote_score

                if new_high_score:
                    result_items = [Result(url=doc.url, title=doc.title, extract=doc.extract,
                                           score=doc.score, term=doc.term, state=doc.state) for doc in new_items]
                    results = Results(api_key=MWMBL_API_KEY, results=result_items, crawler_version=CRAWLER_VERSION)
                    logger.info(f"Posting {len(result_items)} results")
                    response = requests.post(
                        "https://api.mwmbl.org/api/v1/crawler/results", json=results.dict()
                    )
                    logger.info(f"Response: {response.text}")
                    response.raise_for_status()

                new_remote_items = remote_index.retrieve(term, refresh=True)
                # Check how many of our items were indexed
                new_remote_item_urls = {item.url for item in new_remote_items}
                indexed_items = sum(1 for item in new_items if item.url in new_remote_item_urls)
                logger.info(f'Indexed items: {indexed_items}/{len(new_items)} for term "{term}"')

                page_index = local_index.get_key_page_index(term)
                index_pages(index_path, {page_index: new_remote_items}, mark_synced=True)
                logger.info(f"Completed indexing for term {term}")

                new_page_content = local_index.get_page(page_index)
                logger.info(f"Page content: {new_page_content}")
    
    def process_batch_continuously(self):
        """Continuously process batches with error handling."""
        while True:
            self.check_redis()
            try:
                self.process_batch()
            except Exception as err:
                logger.exception(f"Error processing batch: '{err}'")
                time.sleep(10)
    
    def run_indexing_continuously(self):
        """Continuously run indexing with error handling."""
        while True:
            self.check_redis()
            try:
                self.run_indexing()
            except Exception as err:
                logger.exception(f"Error running indexing: '{err}'")
                time.sleep(10)
    
    def run(self):
        """
        Main entry point for the crawler.
        
        Starts multiple worker processes for batch processing and one indexing process.
        Monitors processes and restarts them if they crash.
        """
        validate_environment()  # Validate environment variables before starting
        self.check_redis()
        workers: int = CRAWLER_WORKERS
        assert workers > 0, f"Invalid value for CRAWLER_WORKERS: {workers}"

        batch_processes: list[Process] = []
        for i in range(workers):
            process = Process(target=self.process_batch_continuously)
            process.start()
            batch_processes.append(process)
            time.sleep(5)

        index_process = Process(target=self.run_indexing_continuously)
        index_process.start()

        # Track index process crashes - list of (timestamp, exit_code, pid) tuples
        index_crash_history: list[tuple[datetime, int, int]] = []

        while True:
            if not index_process.is_alive():
                crash_time = datetime.now()
                exit_code = index_process.exitcode or -1
                pid = index_process.pid

                # Record this crash
                index_crash_history.append((crash_time, exit_code, pid))

                # Remove crashes older than 1 hour
                one_hour_ago = crash_time - timedelta(hours=1)
                index_crash_history = [(t, c, p) for t, c, p in index_crash_history if t > one_hour_ago]

                # Check if we've exceeded the crash threshold
                if len(index_crash_history) > 5:
                    crash_details = []
                    for crash_time, exit_code, pid in index_crash_history:
                        crash_details.append(f"  - {crash_time.isoformat()}: pid={pid}, exit_code={exit_code}")

                    error_msg = (f"Index process crashed {len(index_crash_history)} times in the last hour "
                               f"(threshold: 5). Recent crashes:\n" + "\n".join(crash_details))
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

                logger.warning(f"Indexing process [pid={pid}] died with exit code {exit_code}, respawning. "
                             f"Crash count in last hour: {len(index_crash_history)}")
                index_process = Process(target=self.run_indexing_continuously)
                index_process.start()

            for i in range(workers):
                if not batch_processes[i].is_alive():
                    logger.info(f"Batch process [pid={batch_processes[i].pid}] died, respawning.")
                    batch_processes[i] = Process(target=self.process_batch_continuously)
                    batch_processes[i].start()
                    time.sleep(5)

            time.sleep(10)


# Global instance for backward compatibility
_default_crawler = None

def get_default_crawler():
    """Get the default crawler instance."""
    global _default_crawler
    if _default_crawler is None:
        _default_crawler = Crawler()
    return _default_crawler

# Backward compatibility functions
def check_redis():
    """Check Redis connection health (backward compatibility)."""
    return get_default_crawler().check_redis()

def process_batch():
    """Process a single batch (backward compatibility)."""
    return get_default_crawler().process_batch()

def run_indexing():
    """Run indexing (backward compatibility)."""
    return get_default_crawler().run_indexing()

def run():
    """Main entry point (backward compatibility)."""
    return get_default_crawler().run()


if __name__ == "__main__":
    run()
