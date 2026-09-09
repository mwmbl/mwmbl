"""Tests for the Redis key naming the hourly crawl counter.

The key used to be `str()` of a naive datetime. Now that the crawl datetimes are aware,
`str()` would append `+00:00` and the writer and the reader would agree with each other
while both stopped finding the counts already in Redis, silently blanking the hourly
crawl chart.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import fakeredis

from mwmbl.crawler.batch import HashedBatch, Item, ItemContent
from mwmbl.crawler.stats import StatsManager

NO_INDEX_COUNTS = {
    "urls_in_index_daily": {},
    "domains_in_index_daily": {},
    "results_in_index_daily": {},
}


def one_item_batch(crawled_at: datetime) -> HashedBatch:
    timestamp = crawled_at.timestamp()
    item = Item(
        url="https://example.test/a",
        timestamp=timestamp,
        content=ItemContent(title="A", extract="a"),
    )
    return HashedBatch(user_id_hash="a" * 64, timestamp=timestamp, items=[item])


def test_the_hourly_key_is_the_utc_hour_with_no_offset_suffix():
    redis = fakeredis.FakeRedis(decode_responses=True)

    StatsManager(redis).record_batch(one_item_batch(datetime(2026, 9, 8, 13, 42, tzinfo=timezone.utc)))

    assert redis.get("url-count-hour-2026-09-08 13:00:00") == "1"


def test_the_stats_read_back_the_hour_the_batch_was_recorded_into():
    """Midnight, so the hour is in range however far through the day the test runs."""
    today = datetime.now(timezone.utc).date()
    redis = fakeredis.FakeRedis(decode_responses=True)
    stats_manager = StatsManager(redis)

    stats_manager.record_batch(one_item_batch(datetime(today.year, today.month, today.day, tzinfo=timezone.utc)))
    with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS):
        stats = stats_manager.get_stats()

    assert stats.urls_crawled_hourly[0] == 1
