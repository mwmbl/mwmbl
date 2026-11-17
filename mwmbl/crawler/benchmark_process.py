"""The benchmark's `--runner process`: the crawler as a child process of the benchmark.

Kept apart from benchmark.py, which needs only the standard library so that volunteers can run
it against their Compose crawler without installing anything.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv
from redis import Redis

from mwmbl.crawler.benchmark import FETCHES_FIELD, FETCHES_KEY

CRAWLER_EXECUTABLES = ("mwmbl-crawl", "mwmbl.crawl")
STOP_TIMEOUT_SECONDS = 60


class ProcessCrawler:
    """The crawler as a child process of this one, in a process group of its own so that its
    workers are stopped with it. The environment is the child's alone."""

    def __init__(self, command: list[str]):
        self.command = command
        self.process: subprocess.Popen | None = None

    def is_running(self) -> bool:
        return bool(self._existing_crawlers())

    @staticmethod
    def _existing_crawlers() -> list[psutil.Process]:
        def is_crawler(process: psutil.Process) -> bool:
            arguments = (process.info["cmdline"] or [])[:3]
            return process.pid != os.getpid() and any(Path(arg).name in CRAWLER_EXECUTABLES for arg in arguments)

        return [process for process in psutil.process_iter(["cmdline"]) if is_crawler(process)]

    def stop_existing(self) -> None:
        pids = [process.pid for process in self._existing_crawlers()]
        raise SystemExit(
            f"A crawler is already running (pid {pids}). Stop it first: this runner does not know how it was "
            "started, and a second crawler would make every measurement meaningless."
        )

    def start(self, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(self.command, env={**os.environ, **env}, start_new_session=True)

    def ensure_running(self) -> None:
        assert self.process is not None
        exit_code = self.process.poll()
        if exit_code is not None:
            raise RuntimeError(f"the crawler exited with code {exit_code}")

    def fetches(self) -> int:
        redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"))
        return int(redis.hget(FETCHES_KEY, FETCHES_FIELD) or 0)

    def _signal_group(self, signal_number: int) -> bool:
        """Send the signal to the process group, which is the crawler's workers as well as its
        leader. Signal 0 only asks whether any member is left."""
        assert self.process is not None
        try:
            os.killpg(self.process.pid, signal_number)
        except ProcessLookupError:
            return False
        return True

    def stop(self) -> None:
        if self.process is None:
            return
        # Not only while the leader lives: the workers are not daemons, so they outlast a leader
        # that was killed, and would go on crawling beside the next configuration.
        self._signal_group(signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        # poll() reaps the leader, which otherwise stays a zombie and keeps the group alive.
        while self.process.poll() is None or self._signal_group(0):
            if time.monotonic() > deadline:
                self._signal_group(signal.SIGKILL)
                self.process.wait()
                break
            time.sleep(0.2)
        self.process = None

    def restore(self, was_running: bool) -> None:
        """Nothing to restore: a running crawler is refused up front, not stopped."""


def process_crawler(command: list[str], env_file: Path) -> ProcessCrawler:
    """The child inherits this process's environment, so the settings file is loaded into it.
    Variables already in the environment win."""
    load_dotenv(env_file)
    return ProcessCrawler(command)
