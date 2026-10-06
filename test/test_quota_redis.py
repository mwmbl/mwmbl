"""Quota counters and the hourly sync, against a real Redis.

The counter is billed, so it must never hold a request that was refused, not even for an
instant: sync_search_counts copies it to Postgres whenever it runs, and Postgres is the
baseline it is restored from.
"""

import threading
from datetime import datetime, timezone

import pytest
from django.core.cache import cache

from mwmbl.background import sync_search_counts
from mwmbl.models import UsageBucket
from mwmbl.quota import (
    MONTHLY_TTL,
    _combined_search_api_monthly_key,
    _monthly_key,
    delete_all_monthly_keys,
    get_all_combined_search_api_monthly_keys,
    get_all_monthly_keys,
    get_monthly_combined_search_api_count,
    get_monthly_count,
    increment_monthly,
    increment_monthly_combined_search_api_if_below,
)

USER_ID = 42


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="quota", email="quota@example.com", password="x")


def test_a_request_below_the_limit_is_counted(redis_cache):
    assert increment_monthly_combined_search_api_if_below(USER_ID, limit=2) == 1
    assert increment_monthly_combined_search_api_if_below(USER_ID, limit=2) == 2

    assert get_monthly_combined_search_api_count(USER_ID) == 2


def test_a_request_at_the_limit_is_refused_and_not_counted(redis_cache):
    cache.set(_combined_search_api_monthly_key(USER_ID), 2, timeout=MONTHLY_TTL)

    assert increment_monthly_combined_search_api_if_below(USER_ID, limit=2) is None
    assert get_monthly_combined_search_api_count(USER_ID) == 2


def test_a_counter_already_over_the_limit_is_left_alone(redis_cache):
    """A spend limit lowered mid-month leaves the counter above the new limit."""
    cache.set(_combined_search_api_monthly_key(USER_ID), 5, timeout=MONTHLY_TTL)

    assert increment_monthly_combined_search_api_if_below(USER_ID, limit=2) is None
    assert get_monthly_combined_search_api_count(USER_ID) == 5


def test_a_zero_limit_counts_nothing(redis_cache):
    assert increment_monthly_combined_search_api_if_below(USER_ID, limit=0) is None
    assert get_monthly_combined_search_api_count(USER_ID) == 0


def test_a_new_counter_expires_with_the_month(redis_cache):
    increment_monthly_combined_search_api_if_below(USER_ID, limit=10)
    increment_monthly_combined_search_api_if_below(USER_ID, limit=10)

    ttl = redis_cache.ttl(cache.make_key(_combined_search_api_monthly_key(USER_ID)))
    assert MONTHLY_TTL - 5 <= ttl <= MONTHLY_TTL


def test_concurrent_requests_never_pass_the_limit(redis_cache):
    limit = 25
    results = []
    lock = threading.Lock()

    def request():
        count = increment_monthly_combined_search_api_if_below(USER_ID, limit=limit)
        with lock:
            results.append(count)

    threads = [threading.Thread(target=request) for _ in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    counted = sorted(count for count in results if count is not None)
    assert counted == list(range(1, limit + 1))
    assert get_monthly_combined_search_api_count(USER_ID) == limit


def test_the_counter_never_reads_over_the_limit_while_requests_are_refused(redis_cache):
    """This is the read sync_search_counts makes: with increment-then-refund it could catch
    the counter one over the limit, store that in Postgres, and restore it to Redis."""
    limit = 3
    cache.set(_combined_search_api_monthly_key(USER_ID), limit, timeout=MONTHLY_TTL)
    stop = threading.Event()

    def refused_requests():
        while not stop.is_set():
            increment_monthly_combined_search_api_if_below(USER_ID, limit=limit)

    workers = [threading.Thread(target=refused_requests) for _ in range(4)]
    for worker in workers:
        worker.start()
    try:
        reads = [get_monthly_combined_search_api_count(USER_ID) for _ in range(2_000)]
    finally:
        stop.set()
        for worker in workers:
            worker.join()

    assert max(reads) == limit


@pytest.mark.django_db
def test_sync_stores_the_counted_requests_in_postgres(redis_cache, user):
    for _ in range(3):
        increment_monthly_combined_search_api_if_below(user.id, limit=3)
    increment_monthly_combined_search_api_if_below(user.id, limit=3)

    sync_search_counts.now()

    now = datetime.now(timezone.utc)
    bucket = UsageBucket.objects.get(user=user, year=now.year, month=now.month)
    assert bucket.combined_search_count == 3


def test_the_scans_find_the_counters_the_cache_wrote(redis_cache):
    """Redis stores cache keys with a version prefix, which the scan pattern must include."""
    increment_monthly(USER_ID)
    increment_monthly_combined_search_api_if_below(USER_ID, limit=1)

    assert get_all_monthly_keys() == [_monthly_key(USER_ID)]
    assert get_all_combined_search_api_monthly_keys() == [_combined_search_api_monthly_key(USER_ID)]


def test_delete_all_monthly_keys_deletes_the_counters(redis_cache):
    increment_monthly(USER_ID)

    delete_all_monthly_keys()

    assert get_monthly_count(USER_ID) == 0


@pytest.mark.django_db
def test_sync_stores_standard_search_counts_in_postgres(redis_cache, user):
    for _ in range(4):
        increment_monthly(user.id)

    sync_search_counts.now()

    now = datetime.now(timezone.utc)
    assert UsageBucket.objects.get(user=user, year=now.year, month=now.month).count == 4
