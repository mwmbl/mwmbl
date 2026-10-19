"""
Both copies behind each crawler's least agreeing comparisons, kept so an admin can read them.

CrawlerAgreement keeps only the scores, and finding the text in the submitted batches means
knowing where to look. Here the copies are kept while they are at hand, for a fixed number of
the lowest-scoring comparisons per crawler and week: memory stays bounded, and there is no
similarity below which a comparison is judged bad.

They are ranked within buckets of the gap between the two crawls. Pages change, so ranked
together, stale copies of busy pages would crowd out copies crawled minutes apart that differ
by one inserted sentence - the ones most worth reading. Both crawl times are the crawlers' own
claims, so a crawler can put its comparisons in the stale buckets by claiming old ones.

Each comparison is kept under both crawlers, since a low similarity says the copies differ,
not which one is wrong: the honest crawler revisiting a spammed URL holds the spam as the
other copy. The two roles are ranked apart, so that other crawlers' copies cannot push a
crawler's own submissions out of its list, nor its own submissions push out theirs.
"""

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple, Optional

import redis
from django.conf import settings

from mwmbl.indexer.agreement import Agreement
from mwmbl.search_callers import window_start

# The comparisons in a sorted set, scored by agreement_score, and their copies in a hash
# keyed the same, so that a URL compared again replaces its entry rather than adding one.
RANK_KEY = "crawler:worst-agreements:{user_id}:{role}:{window}:{bucket}"
ENTRIES_KEY = RANK_KEY + ":entries"

WORST_PER_BUCKET = 50

WINDOW_DAYS = 7

# This week and the three before it. Titles and extracts are truncated when submitted, so a
# few hundred kilobytes per crawler at most.
KEPT_WINDOWS = 4

SUBMITTER = "submitter"
"""The crawler's copy, compared with another crawler's stored copy it met."""
STORED = "stored"
"""The crawler's stored copy, met by another crawler's."""
ROLES = [SUBMITTER, STORED]


class GapBucket(NamedTuple):
    name: str
    limit: Optional[int]
    """Upper bound of the gap between the two crawls, in seconds, either way round."""
    label: str


GAP_BUCKETS = [
    GapBucket("hour", 60 * 60, "Crawled within an hour of each other"),
    GapBucket("day", 60 * 60 * 24, "Crawled within a day of each other"),
    GapBucket("week", 60 * 60 * 24 * 7, "Crawled within a week of each other"),
    GapBucket("older", None, "Crawled more than a week apart"),
]
UNKNOWN_GAP = GapBucket("unknown", None, "Crawl gap unknown")
BUCKETS = GAP_BUCKETS + [UNKNOWN_GAP]

_redis: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def gap_bucket(last_crawled_delta: Optional[int]) -> GapBucket:
    if last_crawled_delta is None:
        return UNKNOWN_GAP
    gap = abs(last_crawled_delta)
    return next(bucket for bucket in GAP_BUCKETS if bucket.limit is None or gap < bucket.limit)


def agreement_score(agreement: Agreement) -> float:
    """The lower of the title and extract similarities, so that a rewritten title ranks as
    low as a rewritten extract. An empty extract scores zero against anything, so where
    either is empty the titles alone are compared; CrawlerAgreement counts the empty ones."""
    if agreement.extract_empty or agreement.existing_extract_empty:
        return agreement.title_similarity
    return min(agreement.title_similarity, agreement.extract_similarity)


def record_worst_agreements(client: redis.Redis, agreements: list[Agreement], user_id: int, today: date):
    window = window_start(today).isoformat()
    expire_at = datetime.combine(
        window_start(today) + timedelta(days=WINDOW_DAYS * KEPT_WINDOWS), time(), tzinfo=timezone.utc
    )
    keys = {}
    pipeline = client.pipeline()
    for agreement in agreements:
        score = agreement_score(agreement)
        bucket = gap_bucket(agreement.last_crawled_delta).name
        for owner_id, role, entry in _entries(agreement, user_id, score):
            rank_key = RANK_KEY.format(user_id=owner_id, role=role, window=window, bucket=bucket)
            entries_key = ENTRIES_KEY.format(user_id=owner_id, role=role, window=window, bucket=bucket)
            member = f"{entry['other_user_id']} {agreement.url}"
            pipeline.zadd(rank_key, {member: score})
            pipeline.hset(entries_key, member, json.dumps(entry))
            pipeline.expireat(rank_key, expire_at)
            pipeline.expireat(entries_key, expire_at)
            keys[rank_key] = entries_key
    if not keys:
        return
    for rank_key in keys:
        # Lowest first, so everything from this rank on is the best of the rest.
        pipeline.zrange(rank_key, WORST_PER_BUCKET, -1)
    results = pipeline.execute()

    trim = client.pipeline()
    for (rank_key, entries_key), dropped in zip(keys.items(), results[-len(keys) :]):
        if dropped:
            trim.zrem(rank_key, *dropped)
            trim.hdel(entries_key, *dropped)
    trim.execute()


def _entries(agreement: Agreement, user_id: int, score: float) -> list[tuple[int, str, dict]]:
    scores = {
        "url": agreement.url,
        "score": score,
        "title_similarity": agreement.title_similarity,
        "extract_similarity": agreement.extract_similarity,
        "last_crawled_delta": agreement.last_crawled_delta,
    }
    submitted = {"title": agreement.title, "extract": agreement.extract}
    stored = {"title": agreement.existing_title, "extract": agreement.existing_extract}
    return [
        (user_id, SUBMITTER, scores | {"other_user_id": agreement.existing_user_id, "own": submitted, "other": stored}),
        (agreement.existing_user_id, STORED, scores | {"other_user_id": user_id, "own": stored, "other": submitted}),
    ]


def read_worst_agreements(client: redis.Redis, user_id: int, window: date) -> dict[str, dict[str, list[dict]]]:
    """For each role, the kept comparisons of each gap bucket, least agreeing first."""
    slots = [(role, bucket.name) for role in ROLES for bucket in BUCKETS]
    pipeline = client.pipeline()
    for role, bucket in slots:
        key_parts = {"user_id": user_id, "role": role, "window": window.isoformat(), "bucket": bucket}
        pipeline.zrange(RANK_KEY.format(**key_parts), 0, -1)
        pipeline.hgetall(ENTRIES_KEY.format(**key_parts))
    results = pipeline.execute()

    worst = {role: {} for role in ROLES}
    for (role, bucket), members, entries in zip(slots, results[0::2], results[1::2]):
        worst[role][bucket] = [json.loads(entries[member]) for member in members]
    return worst
