"""
Tests for the daily count of blacklisted documents removed from the index.

Retrieval filters blacklisted domains out of results whether or not the background purge
ever removes them, so a broken purge loop is invisible from the search results alone.
This counter is the only signal that the removals are actually happening.
"""
from datetime import datetime
from unittest.mock import patch

import fakeredis

from mwmbl.crawler.stats import BLACKLISTED_REMOVED_COUNT_KEY, LONG_EXPIRE_SECONDS, StatsManager

NO_INDEX_COUNTS = {
    "urls_in_index_daily": {},
    "domains_in_index_daily": {},
    "results_in_index_daily": {},
}


def test_recorded_removals_appear_in_the_stats_for_today():
    redis = fakeredis.FakeRedis(decode_responses=True)
    stats_manager = StatsManager(redis)

    stats_manager.record_blacklisted_removed(3)
    stats_manager.record_blacklisted_removed(4)

    with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS):
        stats = stats_manager.get_stats()

    today = str(datetime.utcnow().date())
    assert stats.blacklisted_results_removed_daily[today] == 7


def test_stats_report_zero_removals_for_days_with_no_purge():
    redis = fakeredis.FakeRedis(decode_responses=True)
    stats_manager = StatsManager(redis)

    with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS):
        stats = stats_manager.get_stats()

    assert len(stats.blacklisted_results_removed_daily) == 30
    assert set(stats.blacklisted_results_removed_daily.values()) == {0}


def test_the_daily_count_expires_so_it_cannot_grow_without_bound():
    redis = fakeredis.FakeRedis(decode_responses=True)
    StatsManager(redis).record_blacklisted_removed(1)

    key = BLACKLISTED_REMOVED_COUNT_KEY.format(date=datetime.utcnow().date())
    assert 0 < redis.ttl(key) <= LONG_EXPIRE_SECONDS


def test_the_purge_task_records_what_it_removed():
    """The count has to come from the purge itself, not from what was queued: documents
    whose domain came off the blacklist while queued are dropped without being removed."""
    redis = fakeredis.FakeRedis(decode_responses=True)
    queued = [object()]

    with patch("mwmbl.background.drain_purge_queue", return_value=queued), \
            patch("mwmbl.background.get_snapshot_blacklist"), \
            patch("mwmbl.background.TinyIndex"), \
            patch("mwmbl.background.queue_size", return_value=0), \
            patch("mwmbl.background.purge_documents", return_value={"bad.test": 2, "worse.test": 3}), \
            patch("mwmbl.background.stats_manager", StatsManager(redis)):
        from mwmbl.background import purge_blacklisted_from_queue
        purge_blacklisted_from_queue.now()

    key = BLACKLISTED_REMOVED_COUNT_KEY.format(date=datetime.utcnow().date())
    assert redis.get(key) == "5"


def test_the_purge_task_records_nothing_when_the_queue_is_empty():
    redis = fakeredis.FakeRedis(decode_responses=True)

    with patch("mwmbl.background.drain_purge_queue", return_value=[]), \
            patch("mwmbl.background.stats_manager", StatsManager(redis)):
        from mwmbl.background import purge_blacklisted_from_queue
        purge_blacklisted_from_queue.now()

    assert redis.get(BLACKLISTED_REMOVED_COUNT_KEY.format(date=datetime.utcnow().date())) is None
