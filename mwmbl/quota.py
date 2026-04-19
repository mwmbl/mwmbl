"""
Quota and rate-limit helpers for the search API.

All counters use the Django cache interface (django.core.cache.cache) so the
backend can be swapped without changing this code.  The only Redis-specific
call is get_all_monthly_keys(), which uses SCAN via django-redis and is only
called by the background flush/reset jobs.
"""
from datetime import datetime, timezone

from django.core.cache import cache

RATE_LIMIT = 5          # maximum requests per second (all tiers)
MONTHLY_TTL = 60 * 60 * 24 * 35   # 35 days in seconds


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _monthly_key(user_id: int, year: int | None = None, month: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    y = year if year is not None else now.year
    m = month if month is not None else now.month
    return f"search:monthly:{user_id}:{y}:{m:02d}"


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
    # add() is atomic: sets key=1 with TTL only if absent (first request this second)
    if cache.add(key, 1, timeout=1):
        return True
    return cache.incr(key) <= RATE_LIMIT


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


# ---------------------------------------------------------------------------
# Key scanning (used by background jobs only — requires django-redis)
# ---------------------------------------------------------------------------

def get_all_monthly_keys() -> list[str]:
    """
    Return all active monthly counter keys for the current month.
    Uses the underlying Redis SCAN command via django-redis.
    Only call this from background tasks, not from request handlers.
    """
    now = datetime.now(timezone.utc)
    pattern = f"search:monthly:*:{now.year}:{now.month:02d}"
    from django_redis import get_redis_connection
    conn = get_redis_connection("default")
    return [k.decode() if isinstance(k, bytes) else k for k in conn.scan_iter(pattern)]


def delete_all_monthly_keys() -> None:
    """
    Delete all monthly counter keys for the current month.
    Used by the monthly reset job.
    """
    keys = get_all_monthly_keys()
    if keys:
        cache.delete_many(keys)
