"""
Tests for "users crawled today" — the count of distinct crawlers that contacted us.

The submission path feeds the same users set that `get_stats` reads back with `scard`
to build `users_crawled_daily`:

`record_results` (the modern API-key-authenticated results endpoint) adds the
account's `username`.

Because writer and reader are in different methods, a change to one side that splits
the key format would silently zero the chart, so these tests pin the two writers to the
same set.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import fakeredis
import pytest

from mwmbl.crawler.batch import HashedBatch, Item, ItemContent, Result, Results
from mwmbl.crawler.stats import StatsManager
from mwmbl.utils import utc_today

NO_INDEX_COUNTS = {
    "urls_in_index_daily": {},
    "domains_in_index_daily": {},
    "results_in_index_daily": {},
}


def make_results(*urls: str) -> Results:
    return Results(results=[Result(url=url, title="t", extract="e") for url in urls])


def make_batch(user_id_hash: str) -> HashedBatch:
    content = ItemContent(title="Example", extract="An example page", links=[], extra_links=[])
    item = Item(
        url="https://example.com/",
        status=200,
        timestamp=datetime.now(timezone.utc).timestamp(),
        content=content,
    )
    return HashedBatch(user_id_hash=user_id_hash, timestamp=item.timestamp, items=[item])


@pytest.mark.django_db
def test_record_results_counts_each_crawler_once_per_day():
    from mwmbl.models import MwmblUser

    redis = fakeredis.FakeRedis(decode_responses=True)
    stats_manager = StatsManager(redis)

    # Create users in the database
    alice = MwmblUser.objects.create_user(username="alice", password="testpass")
    bob = MwmblUser.objects.create_user(username="bob", password="testpass")

    # A crawler submits twice in the same day with the same API-key user.
    stats_manager.record_results(make_results("https://example.com/"), alice)
    stats_manager.record_results(make_results("https://example.com/b", "https://example.com/c"), alice)
    stats_manager.record_results(make_results("https://other.org/"), bob)

    with patch("mwmbl.crawler.stats.get_counts", return_value=NO_INDEX_COUNTS):
        stats = stats_manager.get_stats()

    today = str(utc_today())
    assert stats.users_crawled_daily[today] == 2
