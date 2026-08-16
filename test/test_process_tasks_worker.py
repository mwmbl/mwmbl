"""Tests for the MWMBL_APP=process_tasks worker (mwmbl.main).

The @background functions in mwmbl.background are only ever *scheduled* by
apps.ready() - it writes a Task row and returns. Something has to execute those rows,
and django-background-tasks' answer is `manage.py process_tasks`. Until this worker
existed there was no way to deploy one from the app image, and every periodic task in
production had sat pending, unattempted, for months.
"""
import pytest
from background_task.models import Task
from background_task.tasks import tasks
from django.core.cache import cache

from mwmbl import background, main
from mwmbl.apps import MwmblConfig


@pytest.fixture
def clear_schedule_lock():
    """_schedule_background_tasks() no-ops while the lock is held, and the lock lives in
    the shared Redis cache rather than the test database."""
    cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)
    yield
    cache.delete(MwmblConfig._SCHEDULE_LOCK_KEY)

# The names apps.py schedules Task rows under.
SCHEDULED_TASK_NAMES = [
    "mwmbl.background.sync_search_counts",
    "mwmbl.background.report_usage_to_polar",
]


def test_scheduled_tasks_are_registered_under_the_names_they_are_scheduled_as():
    """The link between the scheduled row and the runnable function is a bare string.

    DBTaskRunner.get_task_to_run skips any row whose task_name is not in the registry,
    silently and without incrementing attempts, so a function renamed or moved out of
    mwmbl.background would not fail - the rows would just stop being picked up and the
    task would quietly never run again. That is the failure this asserts against.
    """
    for task_name in SCHEDULED_TASK_NAMES:
        assert task_name in tasks._tasks


def test_the_registry_points_at_the_real_functions_in_mwmbl_background():
    """process_tasks' own autodiscover() imports <app>/tasks.py, which mwmbl does not
    have - so registration comes from mwmbl.main importing mwmbl.background. Moving these
    functions to a module nothing imports would leave the worker running with an empty
    registry, which no other test would notice."""
    for task_name in SCHEDULED_TASK_NAMES:
        function_name = task_name.rsplit(".", 1)[1]
        assert tasks._tasks[task_name].task_function is getattr(background, function_name).task_function


@pytest.mark.django_db
def test_every_task_apps_schedules_can_actually_be_run_by_the_worker(clear_schedule_lock):
    """The end-to-end invariant, rather than the two lists agreeing by hand.

    apps.ready() writes Task rows; the worker matches them back to functions by name. A
    row whose name is not in the registry is skipped silently and forever, so scheduling
    something the worker cannot run is a failure that produces no error anywhere.
    """
    MwmblConfig._schedule_background_tasks()

    scheduled_names = set(Task.objects.values_list("task_name", flat=True))

    assert scheduled_names  # guard against the assertion below passing vacuously
    assert scheduled_names <= set(tasks._tasks)


@pytest.fixture
def recorded_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "call_command", lambda command, *args, **kwargs: calls.append(command))
    return calls


def test_process_tasks_app_runs_the_queue(recorded_commands, monkeypatch):
    monkeypatch.setenv("MWMBL_APP", "process_tasks")

    main.run()

    assert "process_tasks" in recorded_commands
    # Migrations run first, as they do for every other MWMBL_APP.
    assert recorded_commands.index("migrate") < recorded_commands.index("process_tasks")


def test_unknown_app_still_raises(recorded_commands, monkeypatch):
    monkeypatch.setenv("MWMBL_APP", "not_an_app")

    with pytest.raises(ValueError, match="Unknown MWMBL_APP"):
        main.run()
