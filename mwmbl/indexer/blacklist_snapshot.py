"""A blacklist representation the search path can afford to consult on every query.

The blacklist providers in blacklist_providers.py are built for the indexing path: the
remote-list ones download tens of megabytes and parse them into a Python set of ~1.3M
domain strings. That is fine for a batch job but wrong for search in two ways:

  * Memory. The combined HaGeZi tif_medium + Block List Project adult list is 1,338,063
    unique domains, which costs ~156 MB as a set[str] - and gunicorn runs
    (cpu_count * 2 + 1) workers, so on an 8-core box that would be ~2.6 GB of duplicated
    domain strings.
  * Latency. RemoteListBlacklistProvider fetches lazily, so the first query in each
    worker - and the first query after the module cache expires - would block on a
    multi-megabyte HTTP download with a 60 second timeout. That is exactly the failure
    mode that commit c28be4e was fixing.

So the query path never fetches, parses or holds the domain strings. A background task
(mwmbl.background.refresh_blacklist_snapshot) does that work once, hashes every domain to
a 64-bit integer, and publishes the sorted array to Redis as one ~11 MB blob. Workers
load the blob into a numpy array and answer membership with a vectorised binary search:
10.7 MB resident, and ~94 us to classify the ~200 distinct domains of a typical query -
noise next to the mmap page reads and the Wikipedia call the same request already makes.

Hashing rather than storing the domains is what buys the 15x memory saving. It costs
exactness in principle: two different domains could collide and a clean domain be treated
as blacklisted. With 64-bit hashes over 1.3M entries the chance any given lookup collides
is ~7e-14, i.e. it will not happen.

Only the *remote* lists go in the snapshot. BuiltInRulesBlacklistProvider (the regex,
EXCLUDED_DOMAINS, the numeric-subdomain heuristic and the hn_top_domains trust exemption)
is cheap and stays a local in-process check, so editing mwmbl/settings.py takes effect on
deploy instead of waiting for the next snapshot refresh. Evaluating them as
`built_in_rules OR snapshot` reproduces CombinedBlacklistProvider's semantics exactly.
"""
import hashlib
import threading
import time
from logging import getLogger
from typing import Iterable, Optional

import mmh3
import numpy as np
import redis
from django.conf import settings

from mwmbl.indexer.blacklist import get_default_blacklist_provider
from mwmbl.indexer.blacklist_providers import (
    BlacklistProvider,
    BuiltInRulesBlacklistProvider,
    CombinedBlacklistProvider,
    RemoteListBlacklistProvider,
    domain_and_parents,
)

logger = getLogger(__name__)


SNAPSHOT_KEY = "blacklist:domain-hashes:v1"
SNAPSHOT_VERSION_KEY = "blacklist:domain-hashes:v1:version"

# Hashes are stored little-endian so the blob is portable between machines.
HASH_DTYPE = np.dtype(np.uint64).newbyteorder('<')


_redis: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    """Binary-safe Redis connection - the snapshot blob is not text."""
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL)
    return _redis


def hash_domain(domain: str) -> int:
    return mmh3.hash64(domain, signed=False)[0]


def hash_domains(domains: Iterable[str]) -> np.ndarray:
    return np.fromiter((hash_domain(d) for d in domains), dtype=HASH_DTYPE)


# ---------------------------------------------------------------------------
# Write side - runs in the background task only
# ---------------------------------------------------------------------------

def collect_remote_domains(provider: BlacklistProvider) -> set[str]:
    """Every domain from the remote lists reachable from this provider.

    Walks CombinedBlacklistProvider recursively so callers can just hand over
    get_default_blacklist_provider() without knowing how it is composed. Providers that
    are not remote lists (the built-in rules) have no enumerable domain set and are
    skipped - they are evaluated locally instead, see the module docstring.

    A failed fetch propagates rather than contributing an empty set: the union of the
    lists is what gets published, so one list silently returning nothing would replace
    every worker's snapshot with one missing hundreds of thousands of domains.
    """
    if isinstance(provider, CombinedBlacklistProvider):
        domains = set()
        for sub_provider in provider.providers:
            domains |= collect_remote_domains(sub_provider)
        return domains
    if isinstance(provider, RemoteListBlacklistProvider):
        return provider._get_blacklisted_domains()
    return set()


def build_snapshot(provider: BlacklistProvider) -> bytes:
    """Download/parse the remote lists and return the sorted hash array as bytes."""
    domains = collect_remote_domains(provider)
    all_hashes = hash_domains(domains)
    hashes = np.unique(all_hashes)  # np.unique sorts, which is what we need
    logger.info("Built blacklist snapshot: %d domains, %d unique hashes, %.1f MB",
                len(domains), len(hashes), hashes.nbytes / 1e6)
    return hashes.astype(HASH_DTYPE).tobytes()


def publish_snapshot(blob: bytes, redis_client: Optional[redis.Redis] = None) -> str:
    """Publish the blob and return its version.

    The version is a content hash, so a refresh that produces an identical snapshot does
    not make every worker re-download 11 MB. Blob first, then version: a worker that sees
    a new version is then guaranteed to find the matching blob.
    """
    redis_client = redis_client if redis_client is not None else get_redis()
    version = hashlib.sha256(blob).hexdigest()
    redis_client.set(SNAPSHOT_KEY, blob)
    redis_client.set(SNAPSHOT_VERSION_KEY, version)
    logger.info("Published blacklist snapshot version %s (%.1f MB)", version[:12], len(blob) / 1e6)
    return version


def refresh_snapshot(provider: Optional[BlacklistProvider] = None,
                     redis_client: Optional[redis.Redis] = None) -> str:
    if provider is None:
        provider = get_default_blacklist_provider()
    blob = build_snapshot(provider)
    return publish_snapshot(blob, redis_client)


# ---------------------------------------------------------------------------
# Read side - runs in every search worker
# ---------------------------------------------------------------------------

class SnapshotBlacklist:
    """Membership queries against the published snapshot, plus the local built-in rules.

    One instance per process. Nothing here ever blocks a request on the network: the
    snapshot is refreshed by a daemon thread, and the array is swapped in by plain
    attribute assignment (atomic in CPython), so an in-flight query simply finishes
    against the previous array.
    """

    def __init__(self, built_in_rules: Optional[BlacklistProvider] = None,
                 redis_client: Optional[redis.Redis] = None):
        self._built_in_rules = built_in_rules if built_in_rules is not None else BuiltInRulesBlacklistProvider()
        self._redis = redis_client
        self._array: Optional[np.ndarray] = None
        self._version: Optional[str] = None
        self._last_checked: float = 0.0
        self._loading = False
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._array is not None

    def _get_redis(self) -> redis.Redis:
        return self._redis if self._redis is not None else get_redis()

    def load_now(self) -> bool:
        """Synchronously fetch the snapshot if its version has changed. Returns True if
        the in-memory array was replaced."""
        try:
            client = self._get_redis()
            version = client.get(SNAPSHOT_VERSION_KEY)
        except Exception:
            logger.warning("Could not read blacklist snapshot version from Redis", exc_info=True)
            return False

        if version is None:
            # Production Redis runs allkeys-lru, so the snapshot can be evicted. Keep
            # whatever we already have rather than falling back to an empty array, which
            # would silently disable filtering; the refresh task will republish it.
            logger.warning("Blacklist snapshot missing from Redis (evicted?); keeping the loaded copy")
            return False

        version = version.decode() if isinstance(version, bytes) else version
        if version == self._version:
            return False

        try:
            blob = client.get(SNAPSHOT_KEY)
        except Exception:
            logger.warning("Could not read blacklist snapshot from Redis", exc_info=True)
            return False

        if not blob:
            logger.warning("Blacklist snapshot version %s present but blob is missing", version[:12])
            return False

        if len(blob) % HASH_DTYPE.itemsize != 0:
            # np.frombuffer would raise, and this runs at import time via search_setup, so
            # a truncated blob would stop every web worker from starting. The published
            # snapshot is always a whole number of hashes, so this means a bad write.
            logger.error("Blacklist snapshot version %s is %d bytes, not a multiple of %d; ignoring it",
                         version[:12], len(blob), HASH_DTYPE.itemsize)
            return False

        array = np.frombuffer(blob, dtype=HASH_DTYPE)
        self._array = array
        self._version = version
        logger.info("Loaded blacklist snapshot version %s: %d domains", version[:12], len(array))
        return True

    def _maybe_refresh(self):
        """Kick off a refresh in the background if it has been long enough."""
        interval = settings.BLACKLIST_SNAPSHOT_CHECK_SECONDS
        if time.monotonic() - self._last_checked < interval:
            return

        with self._lock:
            if time.monotonic() - self._last_checked < interval or self._loading:
                return
            self._last_checked = time.monotonic()
            self._loading = True

        if self._array is None:
            # Logged here rather than per query: this is once per check interval, which
            # is often enough to be noticed and rare enough not to drown the logs.
            logger.error("No blacklist snapshot has loaded; filtering on built-in rules only")

        def load():
            try:
                self.load_now()
            finally:
                self._loading = False

        try:
            threading.Thread(target=load, name="blacklist-snapshot-refresh", daemon=True).start()
        except RuntimeError:
            # The thread never ran, so nothing will clear _loading, and _maybe_refresh
            # would then return early for the life of the process - this worker would
            # keep filtering against whatever snapshot it last managed to load.
            self._loading = False
            logger.exception("Could not start the blacklist snapshot refresh thread")

    def filter_blacklisted(self, domains: Iterable[str]) -> set[str]:
        """Return the subset of these domains that are blacklisted."""
        domains = list(dict.fromkeys(domains))  # de-duplicate, keeping order for zip below
        if not domains:
            return set()

        self._maybe_refresh()

        blacklisted = {d for d in domains if self._built_in_rules.is_domain_blacklisted(d)}

        array = self._array  # read once: a background refresh may swap it mid-call
        if array is None:
            return blacklisted

        remaining = [d for d in domains if d not in blacklisted]
        if not remaining:
            return blacklisted

        # The lists hold apex rules, so a domain is blacklisted if it *or any parent* is
        # listed - see domain_and_parents. Every candidate goes into one flat array so
        # this stays a single vectorised search; candidate_domains maps each hit back to
        # the domain that was asked about. A typical host contributes 2-3 candidates.
        candidates = []
        candidate_domains = []
        for domain in remaining:
            for candidate in domain_and_parents(domain):
                candidates.append(candidate)
                candidate_domains.append(domain)

        hashes = hash_domains(candidates)
        positions = np.searchsorted(array, hashes)
        # searchsorted can return len(array) for a hash above every entry; clamp so the
        # lookup below stays in bounds. Index 0 is always a safe stand-in because the
        # equality check is what decides membership.
        positions[positions >= len(array)] = 0
        found = array[positions] == hashes

        blacklisted.update(domain for domain, hit in zip(candidate_domains, found) if hit)
        return blacklisted

    def is_domain_blacklisted(self, domain: str) -> bool:
        return domain in self.filter_blacklisted([domain])


_snapshot_blacklist: Optional[SnapshotBlacklist] = None


def get_snapshot_blacklist() -> SnapshotBlacklist:
    """The per-process singleton, shared by search_setup and the background tasks."""
    global _snapshot_blacklist
    if _snapshot_blacklist is None:
        _snapshot_blacklist = SnapshotBlacklist()
        _snapshot_blacklist.load_now()
    return _snapshot_blacklist
