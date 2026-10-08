"""
Tests for the weekly count of unique URLs in the index.

The scan is spread over many background task runs, each picking up where the last left
off, so what matters is that the slices add up to one correct count however the scan is
cut, and that a new scan only starts once a week.
"""

from datetime import timedelta
from unittest.mock import patch

import fakeredis
import pytest

from mwmbl import count_urls
from mwmbl.count_urls import (
    INDEX_DOMAIN_COUNT_KEY,
    INDEX_RESULT_COUNT_KEY,
    INDEX_SCAN_KEY,
    INDEX_SCAN_LAST_FINISHED_KEY,
    INDEX_SCAN_URL_HLL_KEY,
    INDEX_URL_COUNT_KEY,
    count_urls_step,
    get_counts,
    get_published_counts,
    get_scan_status,
)
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, PageError, TinyIndex
from mwmbl.utils import utc_today

NUM_PAGES = 10


def _document(url, term):
    return Document(title=f"Title of {url}", url=url, extract="An extract", score=1.0, term=term)


@pytest.fixture
def index_path(tmp_path):
    """An index whose URLs repeat across pages, as a URL indexed under several terms does."""
    path = tmp_path / "count.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=NUM_PAGES, page_size=PAGE_SIZE)
    with TinyIndex(Document, str(path), "w") as index:
        index.store_in_page(0, [_document("https://a.test/1", "one"), _document("https://a.test/2", "one")])
        index.store_in_page(3, [_document("https://a.test/1", "two"), _document("https://b.test/1", "two")])
        index.store_in_page(9, [_document("https://c.test/1", "three")])
    return path


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def _count(redis, key):
    return int(redis.get(key.format(date=utc_today())))


def test_a_full_scan_counts_unique_urls_domains_and_results(redis, index_path):
    count_urls_step(redis, index_path, time_budget_seconds=60)

    assert _count(redis, INDEX_URL_COUNT_KEY) == 4
    assert _count(redis, INDEX_DOMAIN_COUNT_KEY) == 3
    assert _count(redis, INDEX_RESULT_COUNT_KEY) == 5
    assert not redis.exists(INDEX_SCAN_KEY)


def test_a_scan_cut_into_slices_gives_the_same_counts(redis, index_path):
    with patch.object(count_urls, "NUM_PAGES_IN_BATCH", 2):
        # A zero budget still reads one batch per run, so this takes five runs.
        for _ in range(4):
            count_urls_step(redis, index_path, time_budget_seconds=0)
            assert redis.get(INDEX_URL_COUNT_KEY.format(date=utc_today())) is None

        count_urls_step(redis, index_path, time_budget_seconds=0)

    assert _count(redis, INDEX_URL_COUNT_KEY) == 4
    assert _count(redis, INDEX_DOMAIN_COUNT_KEY) == 3
    assert _count(redis, INDEX_RESULT_COUNT_KEY) == 5


def test_no_new_scan_starts_within_the_interval(redis, index_path, settings):
    count_urls_step(redis, index_path, time_budget_seconds=60)
    redis.delete(INDEX_URL_COUNT_KEY.format(date=utc_today()))

    count_urls_step(redis, index_path, time_budget_seconds=60)

    assert redis.get(INDEX_URL_COUNT_KEY.format(date=utc_today())) is None
    assert not redis.exists(INDEX_SCAN_KEY)


def test_a_new_scan_starts_once_the_interval_has_passed(redis, index_path, settings):
    last_finished = utc_today() - timedelta(days=settings.INDEX_COUNT_INTERVAL_DAYS)
    redis.set(INDEX_SCAN_LAST_FINISHED_KEY, str(last_finished))

    count_urls_step(redis, index_path, time_budget_seconds=60)

    assert _count(redis, INDEX_URL_COUNT_KEY) == 4
    assert redis.get(INDEX_SCAN_LAST_FINISHED_KEY) == str(utc_today())


def test_a_new_scan_does_not_inherit_the_last_ones_urls(redis, index_path, tmp_path):
    count_urls_step(redis, index_path, time_budget_seconds=60)
    redis.delete(INDEX_SCAN_LAST_FINISHED_KEY)

    with TinyIndex(Document, str(index_path), "w") as index:
        index.store_in_page(0, [])
        index.store_in_page(3, [])

    count_urls_step(redis, index_path, time_budget_seconds=60)

    assert _count(redis, INDEX_URL_COUNT_KEY) == 1
    assert _count(redis, INDEX_RESULT_COUNT_KEY) == 1


@pytest.mark.parametrize("error", [PageError("torn page"), OSError(5, "Input/output error")])
def test_an_unreadable_page_is_skipped_rather_than_ending_the_scan(redis, index_path, error):
    get_page_tuples = TinyIndex._get_page_tuples

    def get_page_tuples_failing_on_page_zero(self, i, term=None):
        if i == 0:
            raise error
        return get_page_tuples(self, i, term)

    with patch.object(TinyIndex, "_get_page_tuples", get_page_tuples_failing_on_page_zero):
        count_urls_step(redis, index_path, time_budget_seconds=60)

    assert _count(redis, INDEX_URL_COUNT_KEY) == 3
    assert _count(redis, INDEX_RESULT_COUNT_KEY) == 3


def test_a_scan_whose_state_is_lost_starts_again(redis, index_path):
    with patch.object(count_urls, "NUM_PAGES_IN_BATCH", 2):
        count_urls_step(redis, index_path, time_budget_seconds=0)
        redis.delete(INDEX_SCAN_KEY)

        count_urls_step(redis, index_path, time_budget_seconds=0)

    assert redis.hget(INDEX_SCAN_KEY, "next_page") == "2"


def test_the_scan_state_expires_if_it_stops_being_advanced(redis, index_path):
    with patch.object(count_urls, "NUM_PAGES_IN_BATCH", 2):
        count_urls_step(redis, index_path, time_budget_seconds=0)

    assert redis.ttl(INDEX_SCAN_KEY) > 0
    assert redis.ttl(INDEX_SCAN_URL_HLL_KEY) > 0


def test_a_batch_another_run_has_already_counted_is_not_counted_again(redis, index_path):
    count_pages = count_urls._count_pages

    def count_pages_raced_by_another_run(redis, index, start_page, end_page):
        # The other run reads and records the same batch first.
        assert count_pages(redis, index, start_page, end_page)
        return count_pages(redis, index, start_page, end_page)

    with patch.object(count_urls, "_count_pages", count_pages_raced_by_another_run):
        count_urls_step(redis, index_path, time_budget_seconds=60)

    assert not redis.exists(INDEX_URL_COUNT_KEY.format(date=utc_today()))

    count_urls_step(redis, index_path, time_budget_seconds=60)

    assert _count(redis, INDEX_RESULT_COUNT_KEY) == 5


def test_each_day_reports_the_latest_count_published_on_or_before_it(redis):
    today = utc_today()
    redis.set(INDEX_URL_COUNT_KEY.format(date=today - timedelta(days=33)), 100)
    redis.set(INDEX_URL_COUNT_KEY.format(date=today - timedelta(days=10)), 200)

    urls_daily = get_counts(redis)["urls_in_index_daily"]

    assert len(urls_daily) == 30
    assert urls_daily[str(today - timedelta(days=29))] == 100
    assert urls_daily[str(today - timedelta(days=11))] == 100
    assert urls_daily[str(today - timedelta(days=10))] == 200
    assert urls_daily[str(today)] == 200


def test_no_counts_are_reported_before_the_first_scan(redis):
    assert get_counts(redis)["urls_in_index_daily"] == {}


def test_the_background_task_does_not_raise_so_it_keeps_repeating():
    from mwmbl import background

    with patch.object(background, "count_urls_step", side_effect=ConnectionError("Redis is down")):
        background.count_index_urls.now()


def test_scan_status_reports_the_progress_of_a_scan_in_progress(redis, index_path):
    with patch.object(count_urls, "NUM_PAGES_IN_BATCH", 4):
        count_urls_step(redis, index_path, time_budget_seconds=0)

    status = get_scan_status(redis, NUM_PAGES)

    assert status["in_progress"]
    assert status["next_page"] == 4
    assert status["num_pages"] == NUM_PAGES
    assert status["percent_done"] == 40
    assert status["num_results"] == 4
    assert status["urls_so_far"] == 3
    assert status["domains_so_far"] == 2
    assert status["last_finished"] is None
    assert status["next_scan_due"] is None


def test_scan_status_after_a_scan_has_finished(redis, index_path, settings):
    count_urls_step(redis, index_path, time_budget_seconds=60)

    status = get_scan_status(redis, NUM_PAGES)

    assert not status["in_progress"]
    assert status["last_finished"] == utc_today()
    assert status["next_scan_due"] == utc_today() + timedelta(days=settings.INDEX_COUNT_INTERVAL_DAYS)


def test_published_counts_list_only_the_days_a_scan_finished(redis, index_path):
    count_urls_step(redis, index_path, time_budget_seconds=60)

    assert get_published_counts(redis, num_days=30) == [{"date": utc_today(), "urls": 4, "domains": 3, "results": 5}]


def test_scan_status_of_an_empty_index_has_no_percent_done(redis, index_path):
    with patch.object(count_urls, "NUM_PAGES_IN_BATCH", 4):
        count_urls_step(redis, index_path, time_budget_seconds=0)

    assert get_scan_status(redis, num_pages=0)["percent_done"] is None


def test_published_counts_skip_a_day_missing_some_of_its_counts(redis):
    redis.set(INDEX_URL_COUNT_KEY.format(date=utc_today()), 10)

    assert get_published_counts(redis, num_days=30) == []
