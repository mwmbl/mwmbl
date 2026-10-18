"""Counting distinct search callers per week, without keeping who they were.

Sizing for a k-anonymous count of query n-grams: how many distinct callers search in a
week, how much of the traffic the busiest carry, and how many search often enough to
matter. A SearxNG instance forwards no client address, so it is one caller however many
people use it. Only searches somebody typed and got an answer to, since those are what
n-grams would be counted from: /complete is keystrokes and /raw is mostly the crawler's
index sync.

A caller is an IPv4 address or an IPv6 /64, stored only as an HMAC under a random salt
made for the week. The salt lives in Redis and dies a day after the week ends, along with
everything keyed on it, so from then on nobody - us included, with SECRET_KEY and the
whole Redis - can tell which address a token was. Before that, summarise_window rolls the
week into a handful of totals and deletes the per-caller keys, so a finished week is
never more than those totals. The week rather than the day because that is the window
n-grams would be published over: tokens cannot be joined across windows, so callers
counted per day could not be added up to size it.
"""

import hashlib
import hmac
import ipaddress
import json
import random
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import redis
from django.http import HttpRequest

IDENTITY_ENDPOINTS = {"legacy_search", "v1_search", "v2_search", "ui_search"}

WINDOW_DAYS = 7

SALT_KEY = "traffic:caller-salt:{window}"
DISTINCT_KEY = "traffic:callers:{window}"
COUNT_KEY = "traffic:caller-counts:{window}"
OVER_KEY = "traffic:callers-over:{window}:{threshold}"
SUMMARY_KEY = "traffic:caller-summary:{window}"

# The per-caller keys outlive the week by a day so the hourly summary has time to run. If it
# never does, they expire anyway: the week is lost, which is the safe way to fail.
RAW_GRACE = timedelta(days=1)

# Thirteen weeks of summaries. They are a few numbers each, so the length is about how far
# back is worth comparing, not memory.
SUMMARY_EXPIRE_SECONDS = 60 * 60 * 24 * 7 * 13

# The memory bound on the per-caller counts, trimmed the way the user agents are. A caller
# the trim drops restarts from zero, which understates the busiest shares slightly; the
# shares are read no further down than the thousandth, well inside what is kept.
TRACKED_CALLERS = 2000
TRIM_PROBABILITY = 1 / 500

# The share of the week's searches carried by its busiest N callers, at each of these N.
SHARE_RANKS = [1, 10, 100, 1000]

# How many callers searched at least this often. Counted as each caller crosses the line,
# since the trimmed counts lose exactly the tail these are about.
THRESHOLDS = [10, 100, 1000]

# A household or a small network gets a /64, so one IPv6 caller is the whole prefix.
IPV6_PREFIX = 64

# 64 bits: collisions are negligible at a few million callers a week.
TOKEN_LENGTH = 16

# This process's copy of each window's salt, so a request costs no extra round trip.
_salts: dict[date, str] = {}


def window_start(day: date) -> date:
    """The Monday starting the week that day is in."""
    return day - timedelta(days=day.weekday())


def _raw_expiry(window: date) -> datetime:
    window_end = datetime.combine(window + timedelta(days=WINDOW_DAYS), time(), tzinfo=timezone.utc)
    return window_end + RAW_GRACE


def client_network(request: HttpRequest) -> str:
    """The caller's address, or its /64 for IPv6.

    The last X-Forwarded-For entry is the one our nginx saw: dokku sets the header to
    $remote_addr, and anything earlier in the list would be the client's own say-so.
    REMOTE_ADDR is absent when uvicorn has no peer address, as on a unix socket, and this
    runs inside a counter that must never fail a search.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    address = forwarded_for.split(",")[-1].strip() if forwarded_for else request.META.get("REMOTE_ADDR", "")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        # Still a caller, just not one with a parseable address.
        return address
    # Without this every IPv4-mapped address would share ::/64.
    if ip.version == 6 and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/{IPV6_PREFIX}", strict=False))
    return str(ip)


def _salt(redis_client: redis.Redis, window: date) -> str:
    """The window's salt, made by whichever process asks first."""
    if window not in _salts:
        salt_key = SALT_KEY.format(window=window)
        pipeline = redis_client.pipeline()
        pipeline.set(salt_key, secrets.token_hex(32), nx=True, exat=_raw_expiry(window))
        pipeline.get(salt_key)
        _salts.clear()
        _salts[window] = pipeline.execute()[1]
    return _salts[window]


def caller_token(salt: str, network: str) -> str:
    return hmac.new(salt.encode(), network.encode(), hashlib.sha256).hexdigest()[:TOKEN_LENGTH]


@dataclass
class QueuedCaller:
    window: date
    token: str
    count_index: int


def queue_caller(redis_client: redis.Redis, pipeline, request: HttpRequest, day: date) -> QueuedCaller:
    """Add one search by this request's caller to the end of the pipeline, which must have
    nothing queued after it."""
    window = window_start(day)
    expiry = _raw_expiry(window)
    salt = _salt(redis_client, window)
    token = caller_token(salt, client_network(request))

    # Every request rewrites the salt if it is missing. That keeps it warm under allkeys-lru,
    # where a key read once a week per process is the first to go, and if it was evicted
    # anyway, puts back the one this process is still hashing with rather than a new one.
    pipeline.set(SALT_KEY.format(window=window), salt, nx=True, exat=expiry)
    distinct_key = DISTINCT_KEY.format(window=window)
    pipeline.pfadd(distinct_key, token)
    pipeline.expireat(distinct_key, expiry)
    count_key = COUNT_KEY.format(window=window)
    pipeline.zincrby(count_key, 1, token)
    pipeline.expireat(count_key, expiry)
    # Counted back from the end, so this needs nothing from whatever else is in the pipeline.
    count_index = -2
    if random.random() < TRIM_PROBABILITY:
        pipeline.zremrangebyrank(count_key, 0, -TRACKED_CALLERS - 1)
        count_index = -3
    return QueuedCaller(window=window, token=token, count_index=count_index)


def record_thresholds(redis_client: redis.Redis, queued: QueuedCaller, results: list) -> None:
    """Note the caller crossing a threshold, which is a second round trip once per caller per
    threshold. A caller the trim dropped crosses again, and the HyperLogLog counts them once."""
    count = int(results[queued.count_index])
    if count not in THRESHOLDS:
        return
    over_key = OVER_KEY.format(window=queued.window, threshold=count)
    pipeline = redis_client.pipeline()
    pipeline.pfadd(over_key, queued.token)
    pipeline.expireat(over_key, _raw_expiry(queued.window))
    pipeline.execute()


def _raw_keys(window: date) -> list[str]:
    over_keys = [OVER_KEY.format(window=window, threshold=threshold) for threshold in THRESHOLDS]
    return [SALT_KEY.format(window=window), DISTINCT_KEY.format(window=window), COUNT_KEY.format(window=window)] + (
        over_keys
    )


def read_summary(redis_client: redis.Redis, window: date) -> dict | None:
    summary = redis_client.get(SUMMARY_KEY.format(window=window))
    return None if summary is None else json.loads(summary)


def read_live(redis_client: redis.Redis, window: date, searches: int) -> dict:
    """A window's totals, read from its per-caller keys.

    searches is the window's served searches from the request counter, which the shares are
    of, so callers the trim dropped still count towards the total they are a share of.
    """
    pipeline = redis_client.pipeline()
    pipeline.pfcount(DISTINCT_KEY.format(window=window))
    pipeline.zrevrange(COUNT_KEY.format(window=window), 0, max(SHARE_RANKS) - 1, withscores=True)
    for threshold in THRESHOLDS:
        pipeline.pfcount(OVER_KEY.format(window=window, threshold=threshold))
    distinct, busiest, *over = pipeline.execute()

    counts = [int(count) for _, count in busiest]
    return {
        "searches": searches,
        "distinct": distinct,
        "busiest_searches": [sum(counts[:rank]) for rank in SHARE_RANKS],
        "over": over,
    }


def summarise_window(redis_client: redis.Redis, window: date, searches: int) -> None:
    """Replace a finished window's per-caller keys with its totals."""
    totals = read_live(redis_client, window, searches)
    pipeline = redis_client.pipeline()
    pipeline.set(SUMMARY_KEY.format(window=window), json.dumps(totals), ex=SUMMARY_EXPIRE_SECONDS)
    pipeline.delete(*_raw_keys(window))
    pipeline.execute()
