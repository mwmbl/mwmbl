"""
Tests for MwmblConfig._schedule_background_tasks().

Covers the cache-lock guard added to prevent duplicate periodic Task rows when
multiple gunicorn worker processes call AppConfig.ready() on startup, and the
set of periodic tasks that are registered - the tasks only exist if ready()
registers them, and registering the same task twice means it runs twice as
often on every deploy.
"""

from unittest.mock import patch

import pytest
from background_task.models import Task
from django.conf import settings
from django.core.cache import cache

from mwmbl.apps import MwmblConfig

SYNC_TASK = "mwmbl.background.sync_search_counts"
POLAR_REPORT_TASK = "mwmbl.background.report_usage_to_polar"
BLACKLIST_SNAPSHOT_TASK = "mwmbl.background.refresh_blacklist_snapshot"
BLACKLIST_PURGE_TASK = "mwmbl.background.purge_blacklisted_from_queue"
WIKI_INDEX_TASK = "mwmbl.background.index_wiki_results_from_queue"

ALL_TASKS = {SYNC_TASK, POLAR_REPORT_TASK, BLACKLIST_SNAPSHOT_TASK, BLACKLIST_PURGE_TASK,
             WIKI_INDEX_TASK}


@pytest.fixture(autouse=True)
def clear_schedule_lock():
    cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)
    yield
    cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)


@pytest.mark.django_db
def test_schedule_background_tasks_creates_all_tasks():
    MwmblConfig._schedule_background_tasks()

    for task_name in ALL_TASKS:
        assert Task.objects.filter(task_name=task_name).count() == 1


@pytest.mark.django_db
def test_schedule_background_tasks_skips_when_lock_already_held():
    """Simulates a second gunicorn worker starting while the first is scheduling."""
    assert cache.add(MwmblConfig._SCHEDULE_LOCK_KEY, "1", timeout=60)

    MwmblConfig._schedule_background_tasks()

    assert Task.objects.filter(task_name__in=ALL_TASKS).count() == 0


@pytest.mark.django_db
def test_schedule_background_tasks_second_call_does_not_duplicate():
    MwmblConfig._schedule_background_tasks()
    cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)  # as if a later worker acquires the lock next
    MwmblConfig._schedule_background_tasks()

    for task_name in ALL_TASKS:
        assert Task.objects.filter(task_name=task_name).count() == 1


@pytest.mark.django_db
def test_schedule_background_tasks_releases_lock_on_failure():
    with patch("mwmbl.background.sync_search_counts", side_effect=RuntimeError("boom")):
        MwmblConfig._schedule_background_tasks()

    assert Task.objects.filter(task_name=SYNC_TASK).count() == 0
    # Lock was released so a later-starting worker isn't blocked for the full TTL.
    assert cache.get(MwmblConfig._SCHEDULE_LOCK_KEY) is None

    MwmblConfig._schedule_background_tasks()
    for task_name in ALL_TASKS:
        assert Task.objects.filter(task_name=task_name).count() == 1


@pytest.mark.django_db
def test_blacklist_tasks_repeat_at_the_configured_intervals():
    MwmblConfig._schedule_background_tasks()

    snapshot = Task.objects.get(task_name=BLACKLIST_SNAPSHOT_TASK)
    purge = Task.objects.get(task_name=BLACKLIST_PURGE_TASK)

    assert snapshot.repeat == settings.BLACKLIST_SNAPSHOT_REFRESH_SECONDS
    assert purge.repeat == settings.BLACKLIST_PURGE_INTERVAL_SECONDS


@pytest.mark.django_db
def test_wiki_index_task_repeats_at_the_configured_interval():
    MwmblConfig._schedule_background_tasks()

    wiki_index = Task.objects.get(task_name=WIKI_INDEX_TASK)

    assert wiki_index.repeat == settings.WIKI_CACHE_INDEX_INTERVAL_SECONDS


@pytest.mark.django_db
def test_a_one_off_snapshot_rebuild_does_not_suppress_the_periodic_one():
    """Approving a domain submission schedules a one-off rebuild under the same task name
    (mwmbl.signals). One of those sitting in the queue at deploy time must not stop the
    six-hourly task being registered - that would silently leave the snapshot to whatever
    approvals happened to trigger."""
    from mwmbl.background import refresh_blacklist_snapshot
    refresh_blacklist_snapshot(schedule=600)

    MwmblConfig._schedule_background_tasks()

    assert Task.objects.filter(task_name=BLACKLIST_SNAPSHOT_TASK, repeat__gt=0).count() == 1
