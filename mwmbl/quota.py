"""
Quota and rate-limit helpers for the search API.

All counters use the Django cache interface (django.core.cache.cache) so the
backend can be swapped without changing this code.  The exceptions need
django-redis: the key scans, which use SCAN and are only called by background
jobs, and increment_monthly_combined_search_api_if_below(), which runs a Lua
script so that the check and the increment are one atomic step.
"""

from datetime import datetime, timezone

from django.core.cache import cache
from django_redis import get_redis_connection

RATE_LIMIT = 5  # maximum requests per second (all tiers)
MONTHLY_TTL = 60 * 60 * 24 * 35  # 35 days in seconds


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def _monthly_key(user_id: int, year: int | None = None, month: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    y = year if year is not None else now.year
    m = month if month is not None else now.month
    return f"search:monthly:{user_id}:{y}:{m:02d}"


def _super_search_monthly_key(user_id: int, year: int | None = None, month: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    y = year if year is not None else now.year
    m = month if month is not None else now.month
    return f"super_search:monthly:{user_id}:{y}:{m:02d}"


def _combined_search_monthly_key(user_id: int, year: int | None = None, month: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    y = year if year is not None else now.year
    m = month if month is not None else now.month
    return f"combined_search:monthly:{user_id}:{y}:{m:02d}"


def _combined_search_api_monthly_key(user_id: int, year: int | None = None, month: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    y = year if year is not None else now.year
    m = month if month is not None else now.month
    return f"combined_search_api:monthly:{user_id}:{y}:{m:02d}"


def _rate_key(user_id: int) -> str:
    return f"search:rate:{user_id}"


# ---------------------------------------------------------------------------
# Rate limiting (fixed-window, 5 req/s)
# ---------------------------------------------------------------------------


def check_rate_limit(user_id: int) -> bool:
    """
    Fixed-window rate limit: at most RATE_LIMIT requests per second.
    Returns True if the request is allowed, False if the limit is exceeded.
    """
    key = _rate_key(user_id)
    if cache.add(key, 1, timeout=1):
        return True
    try:
        count = cache.incr(key)
    except ValueError:
        # The window expired between add() and incr(), and Django's incr() raises on a
        # missing key rather than creating it, so this request opens the next window.
        cache.add(key, 1, timeout=1)
        return True
    return count <= RATE_LIMIT


# ---------------------------------------------------------------------------
# Monthly quota
# ---------------------------------------------------------------------------


def get_monthly_count(user_id: int) -> int:
    """Return the current monthly request count for a user (0 if not set)."""
    return cache.get(_monthly_key(user_id), default=0)


def increment_monthly(user_id: int) -> int:
    """
    Increment the monthly counter and return the new value.
    Sets a 35-day TTL on first use so the key auto-expires.
    """
    key = _monthly_key(user_id)
    # add() is atomic: sets key=1 with TTL only if it doesn't exist
    if cache.add(key, 1, timeout=MONTHLY_TTL):
        return 1
    return cache.incr(key)


def get_monthly_super_search_count(user_id: int) -> int:
    """Return the current monthly super-search request count for a user (0 if not set)."""
    return cache.get(_super_search_monthly_key(user_id), default=0)


def increment_monthly_super_search(user_id: int) -> int:
    """Increment the monthly super-search counter and return the new value."""
    key = _super_search_monthly_key(user_id)
    if cache.add(key, 1, timeout=MONTHLY_TTL):
        return 1
    return cache.incr(key)


def decrement_monthly_super_search(user_id: int) -> None:
    """Refund one super-search increment (e.g. when the request is rejected over-limit).

    Never drops below 0. No-op if the counter is missing.
    """
    key = _super_search_monthly_key(user_id)
    try:
        if cache.get(key, default=0) > 0:
            cache.decr(key)
    except ValueError:
        # decr() raises if the key vanished between the get and the decr; ignore.
        pass


def get_monthly_combined_search_count(user_id: int) -> int:
    """Return the current monthly combined-search request count for a user (0 if not set)."""
    return cache.get(_combined_search_monthly_key(user_id), default=0)


def increment_monthly_combined_search(user_id: int) -> int:
    """Increment the monthly combined-search counter and return the new value."""
    key = _combined_search_monthly_key(user_id)
    if cache.add(key, 1, timeout=MONTHLY_TTL):
        return 1
    return cache.incr(key)


def decrement_monthly_combined_search(user_id: int) -> None:
    """Refund one combined-search increment (e.g. when the request is rejected over-limit).

    Never drops below 0. No-op if the counter is missing.
    """
    key = _combined_search_monthly_key(user_id)
    try:
        if cache.get(key, default=0) > 0:
            cache.decr(key)
    except ValueError:
        # decr() raises if the key vanished between the get and the decr; ignore.
        pass


def get_monthly_combined_search_api_count(user_id: int) -> int:
    """Return the current monthly keyed (billed) combined-search count for a user (0 if not set)."""
    return cache.get(_combined_search_api_monthly_key(user_id), default=0)


# Count one request only if the counter is below the limit, in a single Redis step. An
# increment-then-refund would leave the counter one over the limit between the two steps,
# and an hourly sync reading it then would store, and later bill, a request that was refused.
_INCREMENT_IF_BELOW_LIMIT = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count >= tonumber(ARGV[1]) then
  return -1
end
count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return count
"""


def increment_monthly_combined_search_api_if_below(user_id: int, limit: int) -> int | None:
    """Count one keyed (billed) combined-search request if the user is below `limit`.

    Returns the new count, or None when the user is already at the limit, in which case
    nothing is counted: the counter never goes over the limit, even for an instant.
    Requires the django-redis cache backend.
    """
    key = cache.make_key(_combined_search_api_monthly_key(user_id))
    conn = get_redis_connection("default")
    count = conn.eval(_INCREMENT_IF_BELOW_LIMIT, 1, key, limit, MONTHLY_TTL)
    return None if count == -1 else count


def get_all_monthly_combined_search_counts() -> dict[int, int]:
    """This month's combined-search count for every user who has used it, by user id.

    Scans the keyspace via django-redis, so it is for admin pages and background jobs, not
    the search path.
    """
    now = datetime.now(timezone.utc)
    keys = list(cache.iter_keys(f"combined_search:monthly:*:{now.year}:{now.month:02d}"))
    counts_by_key = cache.get_many(keys)
    # Keys are combined_search:monthly:<user_id>:<year>:<month>.
    return {int(key.split(":")[2]): count for key, count in counts_by_key.items()}


# ---------------------------------------------------------------------------
# Key scanning (used by background jobs only — requires django-redis)
# ---------------------------------------------------------------------------


def _scan_current_month_keys(prefix: str) -> list[str]:
    """Return the cache keys (as passed to cache.get) matching this month's counters.

    Redis holds each cache key under its versioned form (":1:search:...") so the scan
    pattern needs the same prefix, and the prefix comes off the keys it finds.
    """
    now = datetime.now(timezone.utc)
    pattern = cache.make_key(f"{prefix}:monthly:*:{now.year}:{now.month:02d}")
    redis_prefix = cache.make_key("")
    conn = get_redis_connection("default")
    redis_keys = [k.decode() if isinstance(k, bytes) else k for k in conn.scan_iter(pattern)]
    return [k.removeprefix(redis_prefix) for k in redis_keys]


def get_all_monthly_keys() -> list[str]:
    """
    Return all active monthly counter keys for the current month.
    Uses the underlying Redis SCAN command via django-redis.
    Only call this from background tasks, not from request handlers.
    """
    return _scan_current_month_keys("search")


def get_all_combined_search_api_monthly_keys() -> list[str]:
    """The keyed combined-search counterpart of get_all_monthly_keys."""
    return _scan_current_month_keys("combined_search_api")


def delete_all_monthly_keys() -> None:
    """
    Delete all monthly counter keys for the current month.
    Used by the monthly reset job.
    """
    keys = get_all_monthly_keys()
    if keys:
        cache.delete_many(keys)
