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


class StopLoop(Exception):
    """Breaks run_background_tasks' otherwise infinite restart loop."""


@pytest.fixture
def stop_after_two_runs(monkeypatch, recorded_commands):
    def fake_sleep(seconds):
        assert seconds == main.TASK_QUEUE_RESTART_SECONDS
        if recorded_commands.count("process_tasks") >= 2:
            raise StopLoop
    monkeypatch.setattr(main, "sleep", fake_sleep)


def test_the_queue_is_restarted_if_it_ever_exits(recorded_commands, stop_after_two_runs):
    """process_tasks with --duration 0 loops forever and, on Linux, does not even handle
    SIGTERM - its SignalManager only binds that on Windows. So returning means something
    went wrong, and the queue has to come back: the failure being guarded against is the
    tasks silently stopping, which is how they came to be months out of date."""
    with pytest.raises(StopLoop):
        main.run_background_tasks()

    assert recorded_commands == ["process_tasks", "process_tasks"]


class FakeGunicorn:
    """Stands in for BaseApplication so the server branch does not start a real server."""
    runs = 0

    def __init__(self, *args, **kwargs):
        pass

    def run(self):
        FakeGunicorn.runs += 1


@pytest.fixture
def fake_server(monkeypatch):
    FakeGunicorn.runs = 0
    monkeypatch.setattr(main, "BaseApplication", FakeGunicorn)

    spawned = []
    monkeypatch.setattr(main.multiprocessing, "get_context",
                        lambda method: _FakeContext(method, spawned))
    monkeypatch.setenv("MWMBL_APP", "server")
    return spawned


class _FakeContext:
    def __init__(self, method, spawned):
        self.method = method
        self.spawned = spawned

    def Process(self, target, name, daemon):
        self.spawned.append({"method": self.method, "target": target, "name": name, "daemon": daemon})
        return _FakeProcess()


class _FakeProcess:
    pid = 4242

    def start(self):
        pass


def test_server_starts_the_queue_when_enabled(settings, fake_server, recorded_commands):
    settings.RUN_BACKGROUND_TASKS = True

    main.run()

    assert len(fake_server) == 1
    started = fake_server[0]
    assert started["target"] is main.run_background_tasks
    # Spawn, not fork: a forked child would inherit the parent's Postgres and Redis
    # sockets. Daemonic so it dies with the container rather than outliving it.
    assert started["method"] == "spawn"
    assert started["daemon"]
    assert FakeGunicorn.runs == 1


def test_server_does_not_start_the_queue_by_default(settings, fake_server, recorded_commands):
    # Off unless opted in, because beta shares a database and index with production and
    # only one deployment should be running the tasks.
    settings.RUN_BACKGROUND_TASKS = False

    main.run()

    assert fake_server == []
    assert FakeGunicorn.runs == 1


def test_process_tasks_app_runs_the_queue(recorded_commands, stop_after_two_runs, monkeypatch):
    monkeypatch.setenv("MWMBL_APP", "process_tasks")

    with pytest.raises(StopLoop):
        main.run()

    # Migrations run first, as they do for every other MWMBL_APP.
    assert recorded_commands[0] == "migrate"
    assert "process_tasks" in recorded_commands


def test_unknown_app_still_raises(recorded_commands, monkeypatch):
    monkeypatch.setenv("MWMBL_APP", "not_an_app")

    with pytest.raises(ValueError, match="Unknown MWMBL_APP"):
        main.run()
