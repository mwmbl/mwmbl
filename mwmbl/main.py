import logging
import multiprocessing
import os
from time import sleep

import django
from django.core.management import call_command
from gunicorn.app.base import BaseApplication
from redis import Redis

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


def run():
    django.setup()

    from django.conf import settings
    from mwmbl import background
    from mwmbl.redis_url_queue import RedisURLQueue
    from mwmbl.count_urls import count_urls_continuously
    from mwmbl.indexer.update_urls import update_urls_continuously
    from mwmbl.curated_domains import get_curated_domains

    if settings.STATIC_ROOT:
        call_command("collectstatic", "--clear", "--noinput")

    call_command("migrate")

    # DEPRECATED: update_urls, update_batches, copy_indexes and count_urls are no longer
    # deployed. "server" is the only app that runs, and it now also runs the background
    # task queue (see below). They are kept here rather than deleted because the code
    # they call is still reachable from the management commands and the standalone
    # crawler; treat them as unmaintained.
    mwmbl_app = os.environ["MWMBL_APP"]
    if mwmbl_app == "update_urls":
        redis: Redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)
        url_queue = RedisURLQueue(redis, get_curated_domains)
        update_urls_continuously(settings.DATA_PATH, url_queue)
    elif mwmbl_app == "update_batches":
        background.run(settings.DATA_PATH)
    elif mwmbl_app == "copy_indexes":
        background.copy_indexes_continuously()
    elif mwmbl_app == "count_urls":
        count_urls_continuously()
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
                target=run_background_tasks, name="background-tasks", daemon=True)
            process.start()
            logger.info("Started the background task queue (pid %d)", process.pid)

        workers = multiprocessing.cpu_count() * 2 + 1

        class GunicornApp(BaseApplication):
            def load_config(self):
                self.cfg.set("bind", "0.0.0.0:5000")
                self.cfg.set("workers", workers)
                self.cfg.set("worker_class", "uvicorn.workers.UvicornWorker")
                self.cfg.set("loglevel", "warning")
                self.cfg.set("timeout", 120)

            def load(self):
                from mwmbl.asgi import application
                return application

        GunicornApp().run()
    else:
        raise ValueError(f"Unknown MWMBL_APP: {mwmbl_app}")


if __name__ == "__main__":
    run()
