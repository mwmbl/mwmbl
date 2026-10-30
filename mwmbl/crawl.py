import argparse
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from multiprocessing import Process
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import django
import requests
from django.conf import settings
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

FORMAT = "%(process)d:%(levelname)s:%(name)s:%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)
logger = logging.getLogger(__name__)

# The mwmbl imports below reach Django models, so the app registry has to be ready before
# any of them run. The app registry creates the external cache on startup, so the data path
# has to exist before that.
os.environ["DJANGO_SETTINGS_MODULE"] = "mwmbl.settings_crawler"

data_path = Path(settings.DATA_PATH)
print("Data path", data_path)
data_path.mkdir(exist_ok=True, parents=True)

print("Mwmbl crawling statistics: https://mwmbl.org/stats")

django.setup()

from mwmbl.apps import create_index
from mwmbl.crawler.batch import (
    MAX_ERROR_MESSAGE_LENGTH,
    CrawlerErrorReport,
    ErrorContext,
    ErrorFrame,
    HashedBatch,
    Result,
    Results,
)
from mwmbl.crawler.device import get_device_name
from mwmbl.crawler.env_vars import (
    CRAWL_DELAY_SECONDS,
    CRAWL_REPORT_ERRORS,
    CRAWL_SUBMIT_MODE,
    CRAWL_THREADS,
    CRAWLER_WORKERS,
    MWMBL_API_KEY,
    MWMBL_CONTACT_INFO,
    MWMBL_REMOTE_SERVER,
    SUBMIT_MODE_DRY_RUN,
    SUBMIT_MODE_OFF,
)
from mwmbl.crawler.politeness import abandon_domains, release_domains
from mwmbl.crawler.retrieve import CRAWLER_VERSION, crawl_batch
from mwmbl.indexer.blacklist_providers import CombinedBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist, refresh_snapshot
from mwmbl.indexer.index_batches import index_batches, index_pages
from mwmbl.indexer.update_urls import record_urls_in_database
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.redis_url_queue import RedisURLQueue
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.rank import score_result
from mwmbl.utils import prune_request_cache

BATCH_QUEUE_KEY = "batch-queue"
# Reads (RemoteIndex) and writes (results, curated domains) have to agree about which
# deployment they are talking to, or pointing a crawler at beta would send it there for half
# its traffic and to production for the other half.
REMOTE_SERVER = MWMBL_REMOTE_SERVER

# How the crawler names itself to our own API, most of which is the index sync: one raw search
# per term, for the top hundred terms of every indexing pass. The version is there so that
# crawlers running old code can be told apart and asked to upgrade; the username is the
# account's public name, looked up from the API key, so that a crawler causing trouble can be
# traced to an account without its operator's email address ending up in the API's logs and
# user agent counters. The contact address stays in USER_AGENT, which goes only to the
# third-party sites being crawled.
API_PRODUCT = f"mwmbl-crawler/{CRAWLER_VERSION}"
PROJECT_URL = "https://github.com/mwmbl/mwmbl"
USER_URL = f"{REMOTE_SERVER}/api/v1/platform/user"

_username: Optional[str] = None

_curated_domains_cache: set[str] = set()
_curated_domains_fetched_at: float = 0.0
CURATED_DOMAINS_CACHE_SECONDS = 300.0

# How many of a term's new items must be expected to survive in the main index before we
# submit the term at all. See count_new_index_entries for where the expectation comes from.
MIN_NEW_INDEX_ENTRIES = 1

# How long run_indexing may reuse a cached copy of what the main index holds for a term.
# Never expiring it, which is what request_cache does by default, is wrong here twice over:
# the diff below decides what to submit by comparing local items against remote ones, so a
# stale copy submits what the index already has, and a crawler sees a term it will never see
# again often enough that the cache grew by around 800MB a day until the disk filled.
REMOTE_INDEX_CACHE_EXPIRY = timedelta(hours=1)

# The server indexes submitted results before it answers, so allow it well over a typical
# request's time - but not forever.
RESULTS_POST_TIMEOUT_SECONDS = 120

# Two downloads of up to 60 seconds each, then parsing and hashing.
SNAPSHOT_PUBLISH_TIMEOUT_SECONDS = 600


def _fetch_curated_domains() -> set[str]:
    global _curated_domains_cache, _curated_domains_fetched_at
    now = time.monotonic()
    if now - _curated_domains_fetched_at < CURATED_DOMAINS_CACHE_SECONDS:
        return _curated_domains_cache
    try:
        response = requests.get(
            f"{REMOTE_SERVER}/api/v1/crawler/curated-domains",
            timeout=10,
            headers={"User-Agent": api_user_agent(_fetch_username())},
        )
        response.raise_for_status()
        data = response.json()
        _curated_domains_cache = {d["name"] for d in data["domains"]}
        _curated_domains_fetched_at = now
        logger.info(f"Fetched {len(_curated_domains_cache)} curated domains")
    except Exception:
        logger.exception("Failed to fetch curated domains, using cached value")
    return _curated_domains_cache


def _fetch_username() -> Optional[str]:
    """The public username MWMBL_API_KEY belongs to, or None if it cannot be found out.

    Remembered once found, per process. Not remembered when it fails, so a crawler that started
    while the API was unreachable, or before the API accepted keys here, names itself on a
    later pass - that is one request per indexing pass beside the hundred it already makes.
    """
    global _username
    if _username is not None or not MWMBL_API_KEY:
        return _username
    try:
        response = requests.get(
            USER_URL,
            timeout=10,
            headers={"User-Agent": api_user_agent(None), "X-API-Key": MWMBL_API_KEY},
        )
        response.raise_for_status()
        _username = response.json()["username"]
        logger.info(f"Identifying to the index as user {_username}")
    except requests.RequestException:
        logger.warning("Could not look up the username for MWMBL_API_KEY; requests to the API will not include it")
    return _username


def api_user_agent(username: Optional[str]) -> str:
    # Django allows non-ASCII letters in a username, and a header value has to be latin-1.
    identity = f"+{PROJECT_URL}; user {quote(username, safe='@.+-_')}" if username else f"+{PROJECT_URL}"
    return f"{API_PRODUCT} ({identity})"


# How long to wait before reporting the same error from the same process again. The loops
# retry every ten seconds, so a persistent failure would otherwise send a report each time.
ERROR_REPORT_INTERVAL_SECONDS = 3600.0
_error_last_reported_at: dict[tuple[ErrorContext, str, str, int | None], float] = {}


# The server accepts at most this many frames. A traceback deeper than that is almost always
# recursion, where the innermost frames are the ones that say what went wrong.
MAX_ERROR_FRAMES = 200


def _module_path(filename: str) -> str:
    """
    The path of a source file relative to the Python path entry it was imported from.

    An absolute path names the volunteer's home directory, which often carries their name -
    the reason the crawler identifies itself by a generated device name, not its hostname.
    """
    containing_entries = [entry for entry in sys.path if entry and filename.startswith(entry.rstrip("/") + "/")]
    if not containing_entries:
        return os.path.basename(filename)
    longest_entry = max(containing_entries, key=len)
    return os.path.relpath(filename, longest_entry)


def report_error(context: ErrorContext, error: Exception):
    """
    Send an error the crawl loops caught to the Mwmbl server, which passes it on to Sentry.

    The loops catch everything and retry, which keeps a volunteer's crawler running but means
    a crawler failing on every pass looks, from our side, exactly like one that has stopped.
    Reporting goes through our own endpoint rather than straight to Sentry so that volunteers'
    machines only ever talk to Mwmbl, and so each report arrives tied to an API key.
    """
    if not CRAWL_REPORT_ERRORS or CRAWL_SUBMIT_MODE == SUBMIT_MODE_OFF:
        return

    frames = traceback.extract_tb(error.__traceback__)
    exception_type = f"{type(error).__module__}.{type(error).__qualname__}"
    innermost = frames[-1]
    error_key = (context, exception_type, innermost.filename, innermost.lineno)
    now = time.monotonic()
    last_reported_at = _error_last_reported_at.get(error_key)
    if last_reported_at is not None and now - last_reported_at < ERROR_REPORT_INTERVAL_SECONDS:
        return
    _error_last_reported_at[error_key] = now

    # Messages can name local files too - "No such file or directory: '/home/...'".
    message = str(error).replace(str(Path.home()), "~")[:MAX_ERROR_MESSAGE_LENGTH]
    report = CrawlerErrorReport(
        context=context,
        exception_type=exception_type,
        message=message,
        frames=[
            ErrorFrame(filename=_module_path(frame.filename), lineno=frame.lineno, function=frame.name, line=frame.line)
            for frame in frames[-MAX_ERROR_FRAMES:]
        ],
        crawler_version=CRAWLER_VERSION,
        device_name=get_device_name(data_path),
    )
    # Failing to report must not become a second error in the loop that caught the first.
    try:
        response = requests.post(
            f"{REMOTE_SERVER}/api/v1/crawler/errors",
            json=report.dict(),
            headers={"X-API-Key": MWMBL_API_KEY, "User-Agent": api_user_agent(_username)},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Failed to report error to the Mwmbl server")


def count_new_index_entries(term: str, new_items: list[Document], remote_items: list[Document]) -> int:
    """
    Estimate how many of our new local items the main index will actually keep for a term.

    The main index holds a fixed number of bytes per page and drops the tail that does not
    fit, so contributing to a term the index already covers means displacing something. The
    server ranks each term's documents by score_result on the way in and keeps the best of
    them (index_batches.sort_documents, TinyIndex.store), so a new item survives roughly when
    it outranks one of the documents currently occupying the term's slots. Merging our scores
    into the remote ones and counting how many of ours land in the top len(remote_items) is
    that question asked directly. Ties go to the incumbent: matching what is already indexed
    gains the index nothing.

    A term the index has never seen has nothing to displace, so every item that matches the
    term at all is a new entry.

    This is an estimate, not a simulation. The remote page also holds documents for other
    terms that hash to it, which we cannot see, so the real capacity is lower than
    len(remote_items) suggests and blacklisting can drop items server-side. run_indexing logs
    the estimate next to the number the index actually kept, which is how to calibrate it.

    The term is split, not tokenized, which is what the server does to rank a term's
    documents (index_batches.sort_documents) and the only thing that works here: tokenize is
    not a total function on its own output. Index terms come from tokenizing a title, a URL
    or an extract, and one of those can hold an ellipsis mid-text - "Hello… World" gives
    the token "hello…", which tokenize() then reads as a truncated extract and drops the
    last two tokens of, returning []. score_result asserts on an empty term list, so
    re-tokenizing crashed the indexing loop on every batch that carried such a term.
    """
    terms = term.split()
    new_scores = [score_result(terms, item, True) for item in new_items]
    matching_new_scores = [score for score in new_scores if score > 0.0]
    if not remote_items:
        return len(matching_new_scores)

    remote_scores = [score_result(terms, item, True) for item in remote_items]
    candidates = [(score, True) for score in matching_new_scores] + [(score, False) for score in remote_scores]
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
    return sum(1 for _, is_new in candidates[: len(remote_scores)] if is_new)


# Validate environment variables when actually needed
def validate_environment():
    """Validate required environment variables for crawler operation."""
    if CRAWL_SUBMIT_MODE != SUBMIT_MODE_OFF and not MWMBL_API_KEY.strip():
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
        self._snapshot_blacklist = None

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
            self._url_queue = RedisURLQueue(self.redis, _fetch_curated_domains, self.new_blacklist_provider())
        return self._url_queue

    @property
    def snapshot_blacklist(self) -> SnapshotBlacklist:
        """The remote lists as a hash array, ~11MB, where the sets of domains they come from take ~300MB.

        Lazy so that each process loads its own after the fork. The client is not the one above
        because the snapshot is binary and that one decodes responses as text.
        """
        if self._snapshot_blacklist is None:
            self._snapshot_blacklist = SnapshotBlacklist(redis_client=Redis.from_url(self.redis_url))
            self._snapshot_blacklist.load_now()
        return self._snapshot_blacklist

    def new_blacklist_provider(self) -> CombinedBlacklistProvider:
        # A provider of its own because it holds the approved domains from when it first needs
        # them, so one kept for the life of the process would never see a later approval.
        return CombinedBlacklistProvider([self.snapshot_blacklist], get_exempt_domains=_fetch_curated_domains)

    def publish_blacklist_snapshot(self):
        refresh_snapshot(redis_client=Redis.from_url(self.redis_url))

    def refresh_blacklist_snapshot(self):
        """Download the blocklists and publish them for the workers, from a process that then exits.

        Parsing them takes ~300MB, and exiting is what reliably gives that back to the OS.
        """
        process = Process(target=self.publish_blacklist_snapshot)
        process.start()
        process.join(SNAPSHOT_PUBLISH_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            process.join()
            raise RuntimeError(f"Publishing the blacklist snapshot took over {SNAPSHOT_PUBLISH_TIMEOUT_SECONDS}s")
        if process.exitcode != 0:
            raise RuntimeError(f"Publishing the blacklist snapshot failed with exit code {process.exitcode}")

    def check_redis(self):
        """Check Redis connection health.

        redis.exceptions.ConnectionError is not the builtin of that name - it inherits from
        RedisError - so catching only the builtin meant the friendly message never appeared
        for the case it was written for: redis-py wraps a refused socket in its own class.
        """
        try:
            self.redis.ping()
            logger.debug("Redis ping successful")
        except (ConnectionError, RedisConnectionError):
            raise SystemExit(f"Cannot reach Redis at {self.redis_url}. Make sure your Redis server is running.")

    def process_batch(self):
        """
        Process a single batch of URLs by crawling them concurrently with rate limiting.

        This function handles the core crawling workflow:
        1. Gets a batch of URLs from the Redis URL queue
        2. Crawls the URLs on CRAWL_THREADS threads, each waiting CRAWL_DELAY_SECONDS between URLs
        3. Records crawl results in the database for URL tracking
        4. Pushes the completed batch to Redis queue for indexing

        Each batch is processed as a HashedBatch object containing metadata and crawl results.
        """
        user_id = "test"
        urls = self.url_queue.get_batch(user_id)
        if not urls:
            logger.info("Every queued domain was crawled too recently. Sleeping for 10 seconds.")
            time.sleep(10)
            return
        logger.info(f"Processing batch of {len(urls)} URLs on {CRAWL_THREADS} threads")

        try:
            results = crawl_batch(urls, CRAWL_THREADS, CRAWL_DELAY_SECONDS, self.redis)
        except Exception:
            abandon_domains(self.redis, urls)
            raise
        release_domains(self.redis, results)

        js_timestamp = int(time.time() * 1000)
        batch = HashedBatch.parse_obj(
            {
                "user_id_hash": user_id,
                "timestamp": js_timestamp,
                "items": results,
            }
        )
        record_urls_in_database([batch], self.url_queue, self.new_blacklist_provider())

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

        A term is submitted when enough of its new local items are expected to survive in
        the main index - see count_new_index_entries and MIN_NEW_INDEX_ENTRIES. Once a term
        clears that bar all of its new items go, not just the ones that cleared it: the
        server re-tokenizes each submitted document and files it under every term it matches,
        ranking it against that term's existing documents on the way in
        (index_batches.sort_documents), so an item that is weak here cannot jump the queue -
        it loses here and may win on a term we never asked about. Submitting a URL the index
        already holds is useful in its own right: combine_documents keeps the largest
        last_crawled per URL, so the re-submission refreshes the stored crawl date (the one
        the API reports; recrawl scheduling reads FoundURL.last_crawled from the URL
        database, which this path does not touch).
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

        # Once a round is often enough to keep the cache at roughly an hour's worth of terms,
        # and the rounds are minutes apart, so the sweep costs nothing worth measuring.
        prune_request_cache()
        remote_index = RemoteIndex(user_agent=api_user_agent(_fetch_username()), expire_after=REMOTE_INDEX_CACHE_EXPIRY)
        with TinyIndex(Document, index_path, "w") as local_index:
            for term, count in term_new_doc_count.most_common(100):
                logger.info(f"Syncing term {term} with {count} new local items")
                remote_items = remote_index.retrieve(term)
                remote_item_urls = {item.url for item in remote_items}
                local_items = local_index.retrieve(term)
                new_items = [item for item in local_items if item.url not in remote_item_urls]
                logger.info(f"Found {len(new_items)} new items for term {term}")

                estimated_new_entries = count_new_index_entries(term, new_items, remote_items)
                logger.info(
                    f"Estimated {estimated_new_entries} of {len(new_items)} new items would be "
                    f'kept by the main index for term "{term}"'
                )

                if estimated_new_entries >= MIN_NEW_INDEX_ENTRIES:
                    result_items = [
                        Result(
                            url=doc.url,
                            title=doc.title,
                            extract=doc.extract,
                            score=doc.score,
                            term=doc.term,
                            state=doc.state,
                            last_crawled=doc.last_crawled,
                        )
                        for doc in new_items
                    ]
                    if CRAWL_SUBMIT_MODE == SUBMIT_MODE_OFF:
                        logger.info(f"Submission is off, not posting {len(result_items)} results")
                    else:
                        results = Results(
                            results=result_items,
                            crawler_version=CRAWLER_VERSION,
                            device_name=get_device_name(data_path),
                        )
                        results_url = f"{REMOTE_SERVER}/api/v1/crawler/results"
                        if CRAWL_SUBMIT_MODE == SUBMIT_MODE_DRY_RUN:
                            results_url += "?dry_run=true"
                        logger.info(f"Posting {len(result_items)} results to {results_url}")
                        # A post with no timeout that never gets an answer hangs this loop for
                        # good: no exception, so nothing logged or reported, and no more syncing.
                        response = requests.post(
                            results_url,
                            json=results.dict(),
                            headers={"X-API-Key": MWMBL_API_KEY, "User-Agent": api_user_agent(_username)},
                            timeout=RESULTS_POST_TIMEOUT_SECONDS,
                        )
                        logger.info(f"Response: {response.text}")
                        response.raise_for_status()

                new_remote_items = remote_index.retrieve(term, refresh=True)
                # Check how many of our items were indexed
                new_remote_item_urls = {item.url for item in new_remote_items}
                indexed_items = sum(1 for item in new_items if item.url in new_remote_item_urls)
                logger.info(
                    f'Indexed items: {indexed_items}/{len(new_items)} for term "{term}" '
                    f"(estimated {estimated_new_entries})"
                )

                page_index = local_index.get_key_page_index(term)
                index_pages(index_path, {page_index: new_remote_items}, mark_synced=True)
                logger.info(f"Completed indexing for term {term}")

                new_page_content = local_index.get_page(page_index)
                logger.info(f"Page {page_index} now holds {len(new_page_content)} documents")

    def process_batch_continuously(self):
        """Continuously process batches with error handling.

        The health check runs inside the try for the reason everything else does: a Redis
        that answers with an error is a condition to wait out, not to die of. Redis refusing
        every write ("MISCONF ... unable to persist to disk", after a failed background save)
        took out all four crawl workers and the indexing process this way, because
        check_redis only converts a ConnectionError and let the rest through into a loop that
        was not guarding it. An unreachable Redis still ends the process: check_redis raises
        SystemExit for that, and SystemExit is not an Exception.
        """
        while True:
            try:
                self.check_redis()
                self.process_batch()
            except Exception as err:
                logger.exception(f"Error processing batch: '{err}'")
                report_error("batch", err)
                time.sleep(10)

    def run_indexing_continuously(self):
        """Continuously run indexing with error handling.

        The health check is inside the try - see process_batch_continuously. This loop is
        the one that costs the whole crawler: run() gives up and exits once the indexing
        process has died six times in an hour, so a Redis error escaping here stops
        crawling, not just indexing.
        """
        while True:
            try:
                self.check_redis()
                self.run_indexing()
            except Exception as err:
                logger.exception(f"Error running indexing: '{err}'")
                report_error("indexing", err)
                time.sleep(10)

    def run_once(self):
        """
        Crawl one batch and index it, in this process, then return.

        The continuous loops swallow every exception and restart, which is what a
        long-running volunteer crawler wants and useless as a check: the process stays
        alive and reports nothing while every pass fails. This runs the same pipeline
        with errors fatal, so a caller - CI, mainly - can use the exit code.
        """
        validate_environment()
        self.check_redis()
        self.refresh_blacklist_snapshot()
        self.process_batch()
        self.run_indexing()

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

        self.refresh_blacklist_snapshot()
        last_snapshot_refresh = time.monotonic()

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
            if time.monotonic() - last_snapshot_refresh >= settings.BLACKLIST_SNAPSHOT_REFRESH_SECONDS:
                last_snapshot_refresh = time.monotonic()
                # The workers keep the snapshot they have, so a failed refresh costs only staleness.
                try:
                    self.refresh_blacklist_snapshot()
                except RuntimeError:
                    logger.exception("Could not refresh the blacklist snapshot, keeping the current one")

            if not index_process.is_alive():
                crash_time = datetime.now(timezone.utc)
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

                    error_msg = (
                        f"Index process crashed {len(index_crash_history)} times in the last hour "
                        f"(threshold: 5). Recent crashes:\n" + "\n".join(crash_details)
                    )
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

                logger.warning(
                    f"Indexing process [pid={pid}] died with exit code {exit_code}, respawning. "
                    f"Crash count in last hour: {len(index_crash_history)}"
                )
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


def run_once():
    """Run a single crawl and indexing pass (backward compatibility)."""
    return get_default_crawler().run_once()


def main():
    parser = argparse.ArgumentParser(prog="mwmbl-crawl", description="Run the Mwmbl crawler.")
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Crawl one batch, index it and exit, failing on the first error instead of "
            "restarting. Intended for smoke tests, not for crawling."
        ),
    )
    args = parser.parse_args()

    # Volunteers start the crawler with no setup step, so it creates its own index.
    create_index(settings.INDEX_NAME, settings.NUM_PAGES)
    if args.once:
        run_once()
    else:
        run()


if __name__ == "__main__":
    main()
