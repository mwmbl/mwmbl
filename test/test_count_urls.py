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
    INDEX_SCAN_LAST_STARTED_KEY,
    INDEX_URL_COUNT_KEY,
    count_urls_step,
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
    last_started = utc_today() - timedelta(days=settings.INDEX_COUNT_INTERVAL_DAYS)
    redis.set(INDEX_SCAN_LAST_STARTED_KEY, str(last_started))

    count_urls_step(redis, index_path, time_budget_seconds=60)

    assert _count(redis, INDEX_URL_COUNT_KEY) == 4
    assert redis.get(INDEX_SCAN_LAST_STARTED_KEY) == str(utc_today())


def test_a_new_scan_does_not_inherit_the_last_ones_urls(redis, index_path, tmp_path):
    count_urls_step(redis, index_path, time_budget_seconds=60)
    redis.delete(INDEX_SCAN_LAST_STARTED_KEY)

    with TinyIndex(Document, str(index_path), "w") as index:
        index.store_in_page(0, [])
        index.store_in_page(3, [])

    count_urls_step(redis, index_path, time_budget_seconds=60)

    assert _count(redis, INDEX_URL_COUNT_KEY) == 1
    assert _count(redis, INDEX_RESULT_COUNT_KEY) == 1


def test_an_unreadable_page_is_skipped_rather_than_ending_the_scan(redis, index_path):
    get_page = TinyIndex.get_page

    def get_page_failing_on_page_zero(self, i):
        if i == 0:
            raise PageError("torn page")
        return get_page(self, i)

    with patch.object(TinyIndex, "get_page", get_page_failing_on_page_zero):
        count_urls_step(redis, index_path, time_budget_seconds=60)

    assert _count(redis, INDEX_URL_COUNT_KEY) == 3
    assert _count(redis, INDEX_RESULT_COUNT_KEY) == 3
