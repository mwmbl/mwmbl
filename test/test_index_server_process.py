"""The index server's child process under gunicorn, as mwmbl.main runs it."""

import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple

import httpx
import psutil
import pytest

from mwmbl.main import IndexServerProcess
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

# The server as main.run starts it, with a trivial app in place of mwmbl's and a daemon
# child standing in for the task queue. A uvicorn worker that a signal stops dies of it,
# skipping Python's exit handlers; one that has served its max_requests runs them, as it
# would after a crash.
SERVER_SCRIPT = """
import multiprocessing
import sys
import time

from gunicorn.app.base import BaseApplication

from mwmbl.main import IndexServerProcess, run_gunicorn

load_config = BaseApplication.do_load_config


def load_config_with_one_request_per_worker(self):
    load_config(self)
    self.cfg.set("max_requests", 1)


BaseApplication.do_load_config = load_config_with_one_request_per_worker


async def app(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


if __name__ == "__main__":
    socket_path, index_path, bind = sys.argv[1:]
    daemon = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(60,), daemon=True)
    daemon.start()
    index_server = IndexServerProcess(socket_path, [("main", index_path)], 2)
    print(daemon.pid, index_server.process.pid, flush=True)
    run_gunicorn(lambda: app, bind, 2, index_server, 10)
"""

NUM_WORKERS = 2


def _wait_for(condition, what: str, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"Timed out waiting for {what}"
        time.sleep(0.1)


def _alive(process: psutil.Process) -> bool:
    return process.is_running() and process.status() != psutil.STATUS_ZOMBIE


def _is_worker(process: psutil.Process, args: list[str]) -> bool:
    try:
        return _alive(process) and process.cmdline() == args
    except psutil.NoSuchProcess:
        return False


class Gunicorn(NamedTuple):
    master: subprocess.Popen
    daemon: psutil.Process
    index_server: psutil.Process
    workers: Callable[[], list[psutil.Process]]


@pytest.fixture
def gunicorn(tmp_path):
    index_path = str(tmp_path / "index.tinysearch")
    TinyIndex.create(Document, index_path, num_pages=16, page_size=4096)
    script = tmp_path / "serve.py"
    script.write_text(SERVER_SCRIPT)
    args = [sys.executable, str(script), str(tmp_path / "index.sock"), index_path, f"unix:{tmp_path / 'web.sock'}"]
    repo = Path(__file__).parent.parent
    master = subprocess.Popen(args, cwd=repo, env={"PYTHONPATH": str(repo)}, stdout=subprocess.PIPE, text=True)
    try:
        daemon, index_server = (psutil.Process(int(pid)) for pid in master.stdout.readline().split())

        def workers():
            # Forks of the master, unlike the server and multiprocessing's resource tracker.
            return [child for child in psutil.Process(master.pid).children() if _is_worker(child, args)]

        _wait_for(lambda: len(workers()) == NUM_WORKERS, "the workers")
        yield Gunicorn(master, daemon, index_server, workers)
    finally:
        master.kill()
        master.wait()


def test_the_server_outlives_a_worker_and_stops_with_gunicorn(gunicorn, tmp_path):
    first_workers = {worker.pid for worker in gunicorn.workers()}
    web = httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "web.sock")))
    assert web.get("http://mwmbl/").text == "ok"
    _wait_for(lambda: len({worker.pid for worker in gunicorn.workers()} - first_workers) == 1, "a worker to retire")
    worker = gunicorn.workers()[0]
    worker.send_signal(signal.SIGTERM)
    worker.wait(10)
    _wait_for(lambda: len(gunicorn.workers()) == NUM_WORKERS, "a replacement worker")

    assert _alive(gunicorn.index_server) and _alive(gunicorn.daemon)

    gunicorn.master.send_signal(signal.SIGTERM)
    assert gunicorn.master.wait(30) == 0
    assert not _alive(gunicorn.index_server)


def test_gunicorn_stops_with_an_error_when_the_server_dies(gunicorn):
    gunicorn.index_server.kill()

    assert gunicorn.master.wait(30) != 0


def test_starting_fails_when_the_server_exits_at_once(tmp_path):
    with pytest.raises(RuntimeError, match="exited as it started"):
        IndexServerProcess(str(tmp_path / "index.sock"), [("main", str(tmp_path / "missing.tinysearch"))], 2)
