"""Counting calls to the paid search providers, so what they cost can be read off a dashboard.

Counts calls, not cost: the price changes and the history must not, so cost is worked out
from the price in mwmbl.pricing when the numbers are read. A call is counted when it is
sent, not when it succeeds, because a request that times out on our side may still be billed;
failures are counted on top, so the dashboard shows how much of the spend bought nothing.

Like traffic counting, this never raises: a counter that costs a search its results is worse
than no counter.
"""

from datetime import date
from logging import getLogger

from django.conf import settings
from redis import Redis, RedisError

from mwmbl import pricing, traffic
from mwmbl.utils import utc_today

logger = getLogger(__name__)

PROVIDER_STAAN = "staan"

# What the dashboard calls each provider. Staan's results go out as EUSP, so both names.
PROVIDER_LABELS = {
    PROVIDER_STAAN: "Staan (EUSP)",
}

# In GBP, whatever currency the provider bills in.
PRICE_PER_1000_QUERIES_GBP = {
    PROVIDER_STAAN: pricing.STAAN_WEB_SEARCH_PRICE_PER_1000_QUERIES_EUR * pricing.GBP_PER_EUR,
}

USAGE_KEY = "provider-usage:{date}"

# Ninety days so a quarter of spend can be seen. Production Redis runs allkeys-lru (see
# mwmbl/indexer/blacklist_snapshot.py), so this is a ceiling and not a promise.
EXPIRE_SECONDS = 60 * 60 * 24 * 90

CALLS = "calls"
FAILURES = "failures"


def _record(provider: str, outcome: str) -> None:
    if not settings.PROVIDER_USAGE_COUNTING:
        return

    key = USAGE_KEY.format(date=utc_today())
    try:
        pipeline = traffic.get_redis().pipeline()
        pipeline.hincrby(key, f"{provider}:{outcome}", 1)
        pipeline.expire(key, EXPIRE_SECONDS)
        pipeline.execute()
    except RedisError as e:
        logger.warning("Could not count a %s %s: %s", provider, outcome, type(e).__name__)


def record_provider_call(provider: str) -> None:
    """Count a request about to be sent to `provider`."""
    _record(provider, CALLS)


def record_provider_failure(provider: str) -> None:
    """Count a request to `provider` that did not give us a usable answer."""
    _record(provider, FAILURES)


def read_usage(redis: Redis, days: list[date]) -> dict[date, dict[str, dict[str, int]]]:
    """Calls and failures per provider for each of `days`, zero where nothing was counted."""
    pipeline = redis.pipeline()
    for day in days:
        pipeline.hgetall(USAGE_KEY.format(date=day))
    hashes = pipeline.execute()

    usage = {}
    for day, counts in zip(days, hashes):
        usage[day] = {
            provider: {
                CALLS: int(counts.get(f"{provider}:{CALLS}", 0)),
                FAILURES: int(counts.get(f"{provider}:{FAILURES}", 0)),
            }
            for provider in PROVIDER_LABELS
        }
    return usage


def cost_gbp(provider: str, calls: int) -> float:
    return calls * PRICE_PER_1000_QUERIES_GBP[provider] / 1000
