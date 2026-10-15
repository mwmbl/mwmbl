"""
The long-running loops and process entry points in mwmbl.background and mwmbl.main: what
they restart on, what they run, and how the server app is assembled.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mwmbl import background, main
from mwmbl.models import OldIndex


class LoopStopped(BaseException):
    """Ends a while True once the test has seen what it came for; not an Exception, so loops cannot swallow it."""


@pytest.mark.django_db
def test_copy_all_indexes_resumes_after_the_last_copied_page(monkeypatch):
    OldIndex.objects.create(index_path="/old/fresh", last_page_copied=None)
    OldIndex.objects.create(index_path="/old/partial", last_page_copied=9)
    OldIndex.objects.create(index_path="/old/finished", last_page_copied=50)
    calls = []

    def fake_copy(old_path, new_path, start_page, num_pages):
        calls.append((old_path, start_page))
        return start_page if old_path == "/old/finished" else start_page + 5

    monkeypatch.setattr(background, "copy_pages", fake_copy)

    updated = background.copy_all_indexes(Path("/new"))

    assert sorted(calls) == [("/old/finished", 51), ("/old/fresh", 0), ("/old/partial", 10)]
    assert updated == 2
    assert OldIndex.objects.get(index_path="/old/fresh").last_page_copied == 5
    assert OldIndex.objects.get(index_path="/old/partial").last_page_copied == 15
    assert OldIndex.objects.get(index_path="/old/finished").last_page_copied == 50
    assert OldIndex.objects.get(index_path="/old/fresh").last_copied_time <= datetime.now(timezone.utc)


def test_copy_loop_sleeps_when_idle_and_survives_errors(monkeypatch):
    outcomes = iter([RuntimeError("disk gone"), 0])

    def copy_all(path):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(background, "copy_all_indexes", copy_all)
    sleeps = []

    def record_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise LoopStopped

    monkeypatch.setattr(background, "sleep", record_sleep)

    with pytest.raises(LoopStopped):
        background.copy_indexes_continuously()

    assert sleeps == [10, 10]


def test_retraining_task_runs_the_management_command(monkeypatch):
    call_command = MagicMock()
    monkeypatch.setattr(background, "call_command", call_command)

    background.retrain_domain_moderation_model.now()

    call_command.assert_called_once_with("train_domain_moderation_model")


def test_token_flush_task_runs_the_management_command(monkeypatch):
    call_command = MagicMock()
    monkeypatch.setattr(background, "call_command", call_command)

    background.flush_expired_tokens.now()

    call_command.assert_called_once_with("flushexpiredtokens")


def test_seed_crawl_worker_restarts_after_a_failure(monkeypatch):
    runs = []

    def worker():
        runs.append(1)
        if len(runs) == 1:
            raise ConnectionError("redis went away")

    monkeypatch.setattr(main.django, "setup", MagicMock())
    monkeypatch.setattr("mwmbl.indexer.seed_crawl.run_seed_crawl_worker", worker)
    sleeps = []

    def record_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise LoopStopped

    monkeypatch.setattr(main, "sleep", record_sleep)

    with pytest.raises(LoopStopped):
        main.run_seed_crawls()

    assert len(runs) == 2
    assert sleeps == [main.TASK_QUEUE_RESTART_SECONDS] * 2


@pytest.fixture
def run_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(main.django, "setup", MagicMock())
    monkeypatch.setattr(main, "call_command", lambda *args: calls.append(args))
    return calls


def test_the_copy_indexes_app_copies_indexes_after_migrating(monkeypatch, run_environment):
    monkeypatch.setenv("MWMBL_APP", "copy_indexes")
    copy = MagicMock()
    monkeypatch.setattr(background, "copy_indexes_continuously", copy)

    main.run()

    copy.assert_called_once()
    assert ("migrate",) in run_environment


def test_the_process_tasks_app_runs_only_the_queue(monkeypatch, run_environment):
    monkeypatch.setenv("MWMBL_APP", "process_tasks")
    queue = MagicMock()
    monkeypatch.setattr(main, "run_background_tasks", queue)

    main.run()

    queue.assert_called_once()


def test_an_unknown_app_is_an_error(monkeypatch, run_environment):
    monkeypatch.setenv("MWMBL_APP", "nonsense")

    with pytest.raises(ValueError, match="Unknown MWMBL_APP: nonsense"):
        main.run()


@pytest.mark.parametrize("run_background_tasks", [True, False])
def test_the_server_app_starts_helper_processes_only_when_asked(
    monkeypatch, settings, run_environment, run_background_tasks
):
    monkeypatch.setenv("MWMBL_APP", "server")
    settings.RUN_BACKGROUND_TASKS = run_background_tasks
    context = MagicMock()
    context.Process.return_value.pid = 99
    monkeypatch.setattr(main.multiprocessing, "get_context", lambda method: context)
    gunicorn_settings = {}

    class RecordingConfig:
        def set(self, key, value):
            gunicorn_settings[key] = value

    class FakeBaseApplication:
        def __init__(self):
            self.cfg = RecordingConfig()

        def run(self):
            self.load_config()

    monkeypatch.setattr(main, "BaseApplication", FakeBaseApplication)

    main.run()

    assert gunicorn_settings["bind"] == "0.0.0.0:5000"
    assert gunicorn_settings["worker_class"] == "uvicorn.workers.UvicornWorker"
    expected_processes = 2 if run_background_tasks else 0
    assert context.Process.return_value.start.call_count == expected_processes
