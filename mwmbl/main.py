import contextlib
import ctypes
import logging
import multiprocessing
import multiprocessing.connection
import os
import signal
import threading
from time import monotonic, sleep
from typing import Optional

import django
from django.core.management import call_command
from gunicorn.app.base import BaseApplication

from mwmbl_rank import serve_index

logger = logging.getLogger(__name__)

# How long to wait before restarting the task queue if it ever exits. With --duration 0
# process_tasks loops forever and, on Linux, does not even handle SIGTERM (its
# SignalManager only binds that on Windows), so returning at all means something went
# wrong - a database outage propagating out of run_next_task, say. Restarting matters
# because the alternative is the queue silently stopping, which is how every periodic
# task came to be months out of date in the first place.
TASK_QUEUE_RESTART_SECONDS = 10


def run_background_tasks():
    """Run the django-background-tasks queue. Entry point for the child process.

    Started with the 'spawn' method, so this gets a fresh interpreter and has to set
    Django up itself. That is the point: a forked child would inherit the parent's
    Postgres and Redis sockets, and two processes reading from one connection corrupts
    both. Spawning costs a few seconds of startup, once per container.
    """
    django.setup()

    # Importing the module is what registers the @background functions - the decorator
    # runs at import time, and process_tasks' own autodiscover() only looks for
    # <app>/tasks.py, which mwmbl does not have. test_process_tasks_worker.py guards this.
    from mwmbl import background  # noqa: F401

    while True:
        call_command("process_tasks")
        logger.error("Background task queue exited; restarting in %ds", TASK_QUEUE_RESTART_SECONDS)
        sleep(TASK_QUEUE_RESTART_SECONDS)


def run_seed_crawls():
    """Run queued seed crawls - see mwmbl.indexer.seed_crawl. Entry point for the child process.

    A process of its own, rather than a task on the queue above, so that crawls lasting
    minutes never hold up the periodic tasks. Spawned for the same reason that is.
    """
    django.setup()

    from mwmbl.indexer.seed_crawl import run_seed_crawl_worker

    while True:
        try:
            run_seed_crawl_worker()
        except Exception:
            # A crawl's own failure is recorded and survived inside the worker, so this is
            # Redis or the database going away.
            logger.exception("Seed crawl worker failed; restarting in %ds", TASK_QUEUE_RESTART_SECONDS)
        sleep(TASK_QUEUE_RESTART_SECONDS)


PR_SET_PDEATHSIG = 1
INDEX_SERVER_START_SECONDS = 60


def run_index_server(parent_pid: int, socket_path: str, index_paths: list[tuple[str, str]], num_threads: int):
    """Run the Rust index server until SIGTERM. Entry point for the child process.

    It gets SIGTERM if this process dies without stopping it, so it never outlives it. Only
    outside a container does that let it drain: in one, main.py is PID 1, and its death
    SIGKILLs everything in the PID namespace.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM) != 0:
        raise OSError(ctypes.get_errno(), "Could not set the index server's parent death signal")
    # The parent may have died before the signal was set, when nothing would deliver it.
    if os.getppid() != parent_pid:
        return
    serve_index(socket_path, index_paths, num_threads)


class IndexServerProcess:
    """The index server's child process, from the gunicorn master's side.

    gunicorn's master reaps any child of its own with waitpid(-1), the server among them,
    so multiprocessing may never learn its exit code. Everything here waits on the
    process's sentinel instead, which becomes ready when it exits whoever reaps it.
    """

    def __init__(self, socket_path: str, index_paths: list[tuple[str, str]], num_threads: int):
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        # One a server this container ran before left, so that waiting for it means something.
        with contextlib.suppress(FileNotFoundError):
            os.remove(socket_path)
        args = (os.getpid(), socket_path, index_paths, num_threads)
        self.process = multiprocessing.get_context("spawn").Process(
            target=run_index_server, args=args, name="index-server"
        )
        self.process.start()
        self.stopping = threading.Event()
        deadline = monotonic() + INDEX_SERVER_START_SECONDS
        while not os.path.exists(socket_path):
            if self._exited(timeout=0.05):
                raise RuntimeError("The index server exited as it started")
            if monotonic() > deadline:
                self.process.kill()
                raise RuntimeError(f"The index server did not listen within {INDEX_SERVER_START_SECONDS}s")
        logger.info("Started the index server (pid %d)", self.process.pid)

    def _exited(self, timeout: Optional[float]) -> bool:
        return bool(multiprocessing.connection.wait([self.process.sentinel], timeout))

    def watch(self):
        """Stop gunicorn if the server exits unless asked to, so the container restarts:
        without it every write would fail. Run in a thread of the master."""
        self._exited(timeout=None)
        if not self.stopping.is_set():
            logger.error("The index server exited unexpectedly; stopping")
            os.kill(os.getpid(), signal.SIGTERM)

    def stop(self, timeout_seconds: float):
        """Forward the SIGTERM, and wait for the server to apply the writes queued for it."""
        exited_early = self._exited(timeout=0)
        self.stopping.set()
        if exited_early:
            raise RuntimeError("The index server had exited before it was stopped")
        self.process.terminate()
        if not self._exited(timeout=timeout_seconds):
            self.process.kill()
            raise RuntimeError(
                f"The index server did not stop within {timeout_seconds}s; writes queued for it are lost"
            )
        # None when gunicorn reaped it first.
        self.process.join(1)
        logger.info("The index server stopped (exit code %s)", self.process.exitcode)


def run_gunicorn(
    load_application, bind: str, workers: int, index_server: Optional[IndexServerProcess], stop_seconds: float
):
    class GunicornApp(BaseApplication):
        def load_config(self):
            self.cfg.set("bind", bind)
            self.cfg.set("workers", workers)
            self.cfg.set("worker_class", "uvicorn.workers.UvicornWorker")
            self.cfg.set("loglevel", "warning")
            self.cfg.set("timeout", 120)
            # Explicit, since the container's stop timeout must cover it and
            # INDEX_SERVER_STOP_SECONDS - see settings_common.
            self.cfg.set("graceful_timeout", 30)
            self.cfg.set("post_fork", post_fork)
            if index_server is not None:
                # In the master alone, once the workers have finished the requests that
                # write through the server. A finally around run() would also run in each
                # worker as it exits.
                self.cfg.set("on_exit", lambda arbiter: index_server.stop(stop_seconds))

        def load(self):
            return load_application()

    if index_server is not None:
        threading.Thread(target=index_server.watch, name="index-server-watch", daemon=True).start()
    GunicornApp().run()


def post_fork(arbiter, worker):
    # A worker inherits multiprocessing's record of the master's children, and its exit
    # handler would terminate the daemon ones and join the rest: any worker exiting would
    # stop the task queue, the seed crawls and the index server.
    multiprocessing.process._children.clear()  # ty: ignore[unresolved-attribute]


def index_server_paths(settings) -> list[tuple[str, str]]:
    return [
        ("main", f"{settings.DATA_PATH}/{settings.INDEX_NAME}"),
        ("external_cache", f"{settings.DATA_PATH}/{settings.EXTERNAL_CACHE_INDEX_NAME}"),
    ]


def load_mwmbl_application():
    from mwmbl.asgi import application

    return application


def run():
    django.setup()

    from django.conf import settings

    from mwmbl import background

    if settings.STATIC_ROOT:
        call_command("collectstatic", "--clear", "--noinput")

    call_command("migrate")

    # DEPRECATED: copy_indexes is no longer deployed. "server" is the only app that runs,
    # and it now also runs the background task queue (see below). It is kept here rather
    # than deleted because the code it calls is still reachable from the management
    # commands and the standalone crawler; treat it as unmaintained.
    mwmbl_app = os.environ["MWMBL_APP"]
    if mwmbl_app == "copy_indexes":
        background.copy_indexes_continuously()
    elif mwmbl_app == "process_tasks":
        # The task queue on its own, for running it in a container of its own rather than
        # alongside the server. Not currently deployed - RUN_BACKGROUND_TASKS on the
        # server app is how it runs - but it is the way to isolate the queue if the tasks
        # ever outgrow sharing a container with the web workers.
        run_background_tasks()
    elif mwmbl_app == "server":
        if settings.RUN_BACKGROUND_TASKS:
            # Before gunicorn forks, so there is one queue process per container rather
            # than one per worker. daemon=True ties its lifetime to this process, so it
            # goes away when the container stops.
            #
            # Without this, the @background functions in mwmbl.background only ever get
            # *scheduled*: apps.ready() writes a Task row and nothing executes it. Every
            # periodic task in production had been pending, unattempted, for months.
            process = multiprocessing.get_context("spawn").Process(
                target=run_background_tasks, name="background-tasks", daemon=True
            )
            process.start()
            logger.info("Started the background task queue (pid %d)", process.pid)

            seed_crawl_process = multiprocessing.get_context("spawn").Process(
                target=run_seed_crawls, name="seed-crawls", daemon=True
            )
            seed_crawl_process.start()
            logger.info("Started the seed crawl worker (pid %d)", seed_crawl_process.pid)

        # Before gunicorn, like the task queue. Not a daemon: gunicorn stops it once its
        # workers have finished the requests that write through it.
        index_server = (
            IndexServerProcess(
                settings.INDEX_SERVER_SOCKET, index_server_paths(settings), settings.INDEX_SERVER_THREADS
            )
            if settings.RUN_INDEX_SERVER
            else None
        )
        workers = multiprocessing.cpu_count() * 2 + 1
        run_gunicorn(load_mwmbl_application, "0.0.0.0:5000", workers, index_server, settings.INDEX_SERVER_STOP_SECONDS)
    else:
        raise ValueError(f"Unknown MWMBL_APP: {mwmbl_app}")


if __name__ == "__main__":
    run()
