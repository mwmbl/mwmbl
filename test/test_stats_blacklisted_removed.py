"""
Tests for the daily count of blacklisted documents removed from the index.

Retrieval filters blacklisted domains out of results whether or not the background purge
ever removes them, so a broken purge loop is invisible from the search results alone.
This counter is the only signal that the removals are actually happening.
"""

from unittest.mock import patch

import fakeredis
import pytest

from mwmbl.crawler.stats import StatsManager
from mwmbl.utils import utc_today

NO_INDEX_COUNTS = {
    "urls_in_index_daily": {},
    "domains_in_index_daily": {},
    "results_in_index_daily": {},
}


@pytest.fixture
def redis_client():
    """Provide a fakeredis instance for testing."""
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture
def stats_manager(redis_client):
    """Provide a StatsManager with fakeredis."""
    return StatsManager(redis_client=redis_client)


@pytest.mark.django_db
def test_recorded_removals_appear_in_the_stats_for_today(stats_manager):
    stats_manager.record_blacklisted_removed(3)
    stats_manager.record_blacklisted_removed(4)

    with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS):
        stats = stats_manager.get_stats()

    today = str(utc_today())
    assert stats.blacklisted_results_removed_daily[today] == 7


@pytest.mark.django_db
def test_stats_report_zero_removals_for_days_with_no_purge(stats_manager):
    with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS):
        stats = stats_manager.get_stats()

    assert len(stats.blacklisted_results_removed_daily) == 30
    assert set(stats.blacklisted_results_removed_daily.values()) == {0}


@pytest.mark.django_db
def test_the_daily_count_persists_in_postgres():
    """The count is persisted in Postgres, not Redis."""
    stats_manager = StatsManager()
    stats_manager.record_blacklisted_removed(1)

    from mwmbl.models import DailyCrawlerStats

    today = utc_today()
    stat = DailyCrawlerStats.objects.get(date=today)
    assert stat.blacklisted_results_removed == 1


@pytest.mark.django_db
def test_the_purge_task_records_what_it_removed():
    """The count has to come from the purge itself, not from what was queued: documents
    whose domain came off the blacklist while queued are dropped without being removed."""
    queued = [object()]

    with (
        patch("mwmbl.background.drain_purge_queue", return_value=queued),
        patch("mwmbl.background.get_snapshot_blacklist"),
        patch("mwmbl.background.TinyIndex"),
        patch("mwmbl.background.queue_size", return_value=0),
        patch("mwmbl.background.purge_documents", return_value={"bad.test": 2, "worse.test": 3}),
        patch("mwmbl.background.stats_manager", StatsManager()),
    ):
        from mwmbl.background import purge_blacklisted_from_queue

        purge_blacklisted_from_queue.now()

    from mwmbl.models import DailyCrawlerStats

    today = utc_today()
    stat = DailyCrawlerStats.objects.get(date=today)
    assert stat.blacklisted_results_removed == 5


@pytest.mark.django_db
def test_the_purge_task_records_nothing_when_the_queue_is_empty():
    with (
        patch("mwmbl.background.drain_purge_queue", return_value=[]),
        patch("mwmbl.background.stats_manager", StatsManager()),
    ):
        from mwmbl.background import purge_blacklisted_from_queue

        purge_blacklisted_from_queue.now()

    from mwmbl.models import DailyCrawlerStats

    today = utc_today()
    # Should not create a row if nothing was removed
    assert not DailyCrawlerStats.objects.filter(date=today).exists()
