"""
Tests for the 0031 data migration that cleans up duplicate periodic Task
rows left behind by past deploys racing past the (now-fixed) non-atomic
scheduling check in MwmblConfig._schedule_background_tasks().
"""

import importlib

import pytest
from background_task.models import Task
from django.apps import apps as django_apps
from django.utils import timezone

migration_module = importlib.import_module("mwmbl.migrations.0031_dedupe_background_task_schedules")
dedupe_task_schedules = migration_module.dedupe_task_schedules

SYNC_TASK = "mwmbl.background.sync_search_counts"
POLAR_REPORT_TASK = "mwmbl.background.report_usage_to_polar"


def _make_task(task_name, locked_by=None):
    return Task.objects.create(
        task_name=task_name,
        task_params="[[], {}]",
        task_hash="fake-hash",
        run_at=timezone.now(),
        locked_by=locked_by,
    )


@pytest.mark.django_db
def test_dedupe_removes_extra_unlocked_duplicates():
    kept = _make_task(SYNC_TASK)
    _make_task(SYNC_TASK)
    _make_task(SYNC_TASK)

    dedupe_task_schedules(django_apps, None)

    remaining = list(Task.objects.filter(task_name=SYNC_TASK))
    assert len(remaining) == 1
    assert remaining[0].id == kept.id


@pytest.mark.django_db
def test_dedupe_leaves_locked_task_alone():
    _make_task(SYNC_TASK, locked_by="worker-1")
    _make_task(SYNC_TASK)
    _make_task(SYNC_TASK)

    dedupe_task_schedules(django_apps, None)

    remaining = Task.objects.filter(task_name=SYNC_TASK)
    # The locked (in-flight) task is left alone, plus one surviving unlocked duplicate.
    assert remaining.count() == 2
    assert remaining.filter(locked_by="worker-1").exists()


@pytest.mark.django_db
def test_dedupe_is_idempotent_and_leaves_single_task_alone():
    task = _make_task(POLAR_REPORT_TASK)

    dedupe_task_schedules(django_apps, None)
    dedupe_task_schedules(django_apps, None)

    remaining = list(Task.objects.filter(task_name=POLAR_REPORT_TASK))
    assert len(remaining) == 1
    assert remaining[0].id == task.id


@pytest.mark.django_db
def test_dedupe_only_touches_named_periodic_tasks():
    _make_task("some.other.task")
    _make_task("some.other.task")

    dedupe_task_schedules(django_apps, None)

    assert Task.objects.filter(task_name="some.other.task").count() == 2
