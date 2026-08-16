"""The periodic tasks only exist if apps.ready() registers them, and registering the same
task twice means it runs twice as often on every deploy."""
import pytest
from background_task.models import Task
from django.conf import settings

from mwmbl.apps import MwmblConfig

BLACKLIST_TASKS = {
    "mwmbl.background.refresh_blacklist_snapshot",
    "mwmbl.background.purge_blacklisted_from_queue",
}


@pytest.mark.django_db
def test_blacklist_tasks_are_scheduled():
    MwmblConfig._schedule_background_tasks()

    assert BLACKLIST_TASKS <= set(Task.objects.values_list("task_name", flat=True))


@pytest.mark.django_db
def test_scheduling_twice_does_not_duplicate_tasks():
    MwmblConfig._schedule_background_tasks()
    MwmblConfig._schedule_background_tasks()

    for task_name in BLACKLIST_TASKS:
        assert Task.objects.filter(task_name=task_name).count() == 1


@pytest.mark.django_db
def test_blacklist_tasks_repeat_at_the_configured_intervals():
    MwmblConfig._schedule_background_tasks()

    snapshot = Task.objects.get(task_name="mwmbl.background.refresh_blacklist_snapshot")
    purge = Task.objects.get(task_name="mwmbl.background.purge_blacklisted_from_queue")

    assert snapshot.repeat == settings.BLACKLIST_SNAPSHOT_REFRESH_SECONDS
    assert purge.repeat == settings.BLACKLIST_PURGE_INTERVAL_SECONDS
