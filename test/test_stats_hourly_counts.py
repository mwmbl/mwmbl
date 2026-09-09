"""
Tests for the hourly crawl counter behind the "URLs crawled" chart.

The key is a formatted datetime, so it is the one daily counter whose Redis key changes
if the datetime it is built from becomes timezone-aware: `str()` of an aware datetime
carries a `+00:00` suffix. Writer and reader are in different methods, so getting this
wrong on one side alone empties the chart silently.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import fakeredis

from mwmbl.crawler.batch import HashedBatch, Item, ItemContent
from mwmbl.crawler.stats import URL_HOUR_COUNT_KEY, StatsManager, hour_count_key

NO_INDEX_COUNTS = {
    "urls_in_index_daily": {},
    "domains_in_index_daily": {},
    "results_in_index_daily": {},
}


def make_batch(crawled_at: datetime) -> HashedBatch:
    content = ItemContent(title="Example", extract="An example page", links=[], extra_links=[])
    item = Item(url="https://example.com/", status=200, timestamp=crawled_at.timestamp(), content=content)
    return HashedBatch(user_id_hash="a" * 64, timestamp=crawled_at.timestamp(), items=[item])


def test_the_hour_key_has_no_timezone_offset_in_it():
    aware = datetime(2026, 9, 8, 13, 45, tzinfo=timezone.utc)
    assert hour_count_key(aware) == "url-count-hour-2026-09-08 13:00:00"


def test_a_recorded_batch_is_counted_against_the_utc_hour_it_was_crawled_in():
    redis = fakeredis.FakeRedis(decode_responses=True)
    crawled_at = datetime(2026, 9, 8, 13, 45, tzinfo=timezone.utc)

    with patch("mwmbl.crawler.stats.URLDatabase"):
        StatsManager(redis).record_batch(make_batch(crawled_at))

    assert redis.get(URL_HOUR_COUNT_KEY.format(hour="2026-09-08 13:00:00")) == "1"


def test_the_stats_read_back_the_hour_key_the_batch_wrote():
    """The chart is built by get_stats reading keys record_batch wrote, so the two have to
    agree on the format - not just each be self-consistent."""
    redis = fakeredis.FakeRedis(decode_responses=True)
    now = datetime.now(timezone.utc)

    with patch("mwmbl.crawler.stats.URLDatabase"):
        stats_manager = StatsManager(redis)
        stats_manager.record_batch(make_batch(now))

        with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS):
            stats = stats_manager.get_stats()

    assert stats.urls_crawled_hourly[now.hour] == 1
