"""
Both copies behind each crawler's least agreeing comparisons, kept so an admin can read them.

CrawlerAgreement keeps only the scores, and finding the text in the submitted batches means
knowing where to look. Here the copies are kept while they are at hand, for a fixed number of
the lowest-scoring comparisons per crawler and week: memory stays bounded, and there is no
similarity below which a comparison is judged bad.

They are ranked within buckets of the gap between the two crawls. Pages change, so ranked
together, stale copies of busy pages would crowd out copies crawled minutes apart that differ
by one inserted sentence - the ones most worth reading.

Each comparison is kept under both crawlers, since a low similarity says the copies differ,
not which one is wrong: the honest crawler revisiting a spammed URL holds the spam as the
other copy.
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import redis
from django.conf import settings

from mwmbl.indexer.agreement import Agreement
from mwmbl.search_callers import window_start

WORST_KEY = "crawler:worst-agreements:{user_id}:{window}:{bucket}"

WORST_PER_BUCKET = 50

WINDOW_DAYS = 7

# This week and the three before it. A few hundred kilobytes per crawler at most.
KEPT_WINDOWS = 4

# Upper bounds of the gap between the two crawls, in seconds, either way round.
GAP_BUCKETS = {
    "hour": 60 * 60,
    "day": 60 * 60 * 24,
    "week": 60 * 60 * 24 * 7,
    "older": None,
}
UNKNOWN_GAP = "unknown"
BUCKETS = list(GAP_BUCKETS) + [UNKNOWN_GAP]

_redis: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def gap_bucket(last_crawled_delta: Optional[int]) -> str:
    if last_crawled_delta is None:
        return UNKNOWN_GAP
    gap = abs(last_crawled_delta)
    return next(name for name, limit in GAP_BUCKETS.items() if limit is None or gap < limit)


def record_worst_agreements(client: redis.Redis, agreements: list[Agreement], user_id: int, today: date):
    window = window_start(today)
    expire_at = datetime.combine(window + timedelta(days=WINDOW_DAYS * KEPT_WINDOWS), time(), tzinfo=timezone.utc)
    pipeline = client.pipeline()
    for agreement in agreements:
        # An empty extract scores zero against anything, so these would fill every bucket.
        # CrawlerAgreement counts them.
        if agreement.extract_empty or agreement.existing_extract_empty:
            continue
        bucket = gap_bucket(agreement.last_crawled_delta)
        for owner_id, entry in _entries(agreement, user_id):
            key = WORST_KEY.format(user_id=owner_id, window=window.isoformat(), bucket=bucket)
            pipeline.zadd(key, {json.dumps(entry, sort_keys=True): agreement.extract_similarity})
            # Lowest first, so everything from this rank on is the best of the rest.
            pipeline.zremrangebyrank(key, WORST_PER_BUCKET, -1)
            pipeline.expireat(key, expire_at)
    pipeline.execute()


def _entries(agreement: Agreement, user_id: int) -> list[tuple[int, dict]]:
    scores = {
        "url": agreement.url,
        "title_similarity": agreement.title_similarity,
        "extract_similarity": agreement.extract_similarity,
        "last_crawled_delta": agreement.last_crawled_delta,
    }
    submitted = {"title": agreement.title, "extract": agreement.extract}
    stored = {"title": agreement.existing_title, "extract": agreement.existing_extract}
    return [
        (
            user_id,
            scores
            | {"role": "submitter", "other_user_id": agreement.existing_user_id, "own": submitted, "other": stored},
        ),
        (
            agreement.existing_user_id,
            scores | {"role": "stored", "other_user_id": user_id, "own": stored, "other": submitted},
        ),
    ]


def read_worst_agreements(client: redis.Redis, user_id: int, window: date) -> dict[str, list[dict]]:
    """The kept comparisons of each gap bucket, least agreeing first."""
    pipeline = client.pipeline()
    for bucket in BUCKETS:
        pipeline.zrange(WORST_KEY.format(user_id=user_id, window=window.isoformat(), bucket=bucket), 0, -1)
    members_by_bucket = pipeline.execute()
    return {bucket: [json.loads(member) for member in members] for bucket, members in zip(BUCKETS, members_by_bucket)}
