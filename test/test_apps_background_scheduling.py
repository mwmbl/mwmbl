"""
Tests for MwmblConfig._schedule_background_tasks().

Covers the cache-lock guard added to prevent duplicate periodic Task rows
when multiple gunicorn worker processes call AppConfig.ready() on startup.
"""

from unittest.mock import patch

import pytest
from background_task.models import Task
from django.core.cache import cache

from mwmbl.apps import MwmblConfig

SYNC_TASK = "mwmbl.background.sync_search_counts"
POLAR_REPORT_TASK = "mwmbl.background.report_usage_to_polar"


@pytest.fixture(autouse=True)
def clear_schedule_lock():
    cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)
    yield
    cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)


@pytest.mark.django_db
def test_schedule_background_tasks_creates_both_tasks():
    MwmblConfig._schedule_background_tasks()

    assert Task.objects.filter(task_name=SYNC_TASK).count() == 1
    assert Task.objects.filter(task_name=POLAR_REPORT_TASK).count() == 1


@pytest.mark.django_db
def test_schedule_background_tasks_skips_when_lock_already_held():
    """Simulates a second gunicorn worker starting while the first is scheduling."""
    assert cache.add(MwmblConfig._SCHEDULE_LOCK_KEY, "1", timeout=60)

    MwmblConfig._schedule_background_tasks()

    assert Task.objects.filter(task_name=SYNC_TASK).count() == 0
    assert Task.objects.filter(task_name=POLAR_REPORT_TASK).count() == 0


@pytest.mark.django_db
def test_schedule_background_tasks_second_call_does_not_duplicate():
    MwmblConfig._schedule_background_tasks()
    cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)  # as if a later worker acquires the lock next
    MwmblConfig._schedule_background_tasks()

    assert Task.objects.filter(task_name=SYNC_TASK).count() == 1
    assert Task.objects.filter(task_name=POLAR_REPORT_TASK).count() == 1


@pytest.mark.django_db
def test_schedule_background_tasks_releases_lock_on_failure():
    with patch("mwmbl.background.sync_search_counts", side_effect=RuntimeError("boom")):
        MwmblConfig._schedule_background_tasks()

    assert Task.objects.filter(task_name=SYNC_TASK).count() == 0
    # Lock was released so a later-starting worker isn't blocked for the full TTL.
    assert cache.get(MwmblConfig._SCHEDULE_LOCK_KEY) is None

    MwmblConfig._schedule_background_tasks()
    assert Task.objects.filter(task_name=SYNC_TASK).count() == 1
    assert Task.objects.filter(task_name=POLAR_REPORT_TASK).count() == 1
