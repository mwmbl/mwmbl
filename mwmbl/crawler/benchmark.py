"""Find the crawler configuration that earns this machine the highest index score.

Runs every combination of process count and thread count, one at a time: stop the crawler,
start it with that combination, let it warm up, then measure for a fixed time. The winner is
the combination that got the most results into the Mwmbl index.

What is measured is how many results the crawler's device submitted between the end of warmup
and the end of the measurement, as counted by the server from `GET /api/v1/crawler/device-results`.
Each submission adds the number of URLs it put in the index that were not there already, so this
is exactly what the crawler contributed in the window. The devices page's "last 24 hours" figure
is a rolling window shared by every configuration that ran in it, so it is not used.

Nothing here says "Python": it needs only a way to start and stop the crawler with CRAWLER_WORKERS
and CRAWL_THREADS set, and the server's API. It authenticates with the crawler's MWMBL_API_KEY,
which must be set in this environment as for the crawler.

Run as `mwmbl benchmark` or `python -m mwmbl.crawler.benchmark`.
"""

import argparse
import csv
import itertools
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

import psutil
import requests

from mwmbl.crawler.device import get_device_name
from mwmbl.crawler.env_vars import MWMBL_API_KEY, MWMBL_DEVICE_NAME, MWMBL_REMOTE_SERVER

DEFAULT_PROCESS_COUNTS = [1, 2, 3, 4, 5]
DEFAULT_THREAD_COUNTS = [10, 25, 50, 100]
DEFAULT_WARMUP = "5m"
DEFAULT_DURATION = "30m"

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_INTERRUPTED = "INTERRUPTED"

COMPOSE_SERVICE = "mwmbl-crawler"
CRAWLER_EXECUTABLES = ("mwmbl-crawl", "mwmbl.crawl")
STOP_TIMEOUT_SECONDS = 60
PROGRESS_INTERVAL_SECONDS = 60

# Rough, and only used to skip combinations that would obviously exhaust memory: the indexing
# process and Redis, then each crawl worker. Memory scales with processes, not threads. The
# index score still decides the winner of every combination that is run.
BASE_MEMORY_MB = 600
DEFAULT_MEMORY_PER_PROCESS_MB = 400

CSV_COLUMNS = [
    "timestamp",
    "hostname",
    "device_name",
    "crawler_version",
    "processes",
    "threads",
    "warmup_seconds",
    "duration_seconds",
    "index_score",
    "status",
    "error",
]


@dataclass(frozen=True)
class Configuration:
    processes: int
    threads: int

    def env(self) -> dict[str, str]:
        return {"CRAWLER_WORKERS": str(self.processes), "CRAWL_THREADS": str(self.threads)}


@dataclass
class Result:
    configuration: Configuration
    status: str
    index_score: int | None = None
    crawler_version: str = "unknown"
    error: str = ""
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DeviceReading:
    known: bool
    results_since: int
    server_time: float
    crawler_version: str


def parse_counts(text: str) -> list[int]:
    counts = [int(part) for part in text.split(",")]
    if not counts or any(count < 1 for count in counts):
        raise argparse.ArgumentTypeError(f"expected positive integers separated by commas, got {text!r}")
    return list(dict.fromkeys(counts))


def parse_duration(text: str) -> int:
    match = re.fullmatch(r"(\d+)([smh])", text)
    if match is None:
        raise argparse.ArgumentTypeError(f"expected a duration like 90s, 5m or 1h, got {text!r}")
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


def format_duration(seconds: int) -> str:
    for unit_seconds, unit in ((3600, "h"), (60, "m")):
        if seconds and seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{unit}"
    return f"{seconds}s"


def all_configurations(process_counts: list[int], thread_counts: list[int]) -> list[Configuration]:
    return [Configuration(p, t) for p, t in itertools.product(process_counts, thread_counts)]


def total_memory_mb() -> float:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**20


class Crawler(Protocol):
    def is_running(self) -> bool: ...

    def stop_existing(self) -> None: ...

    def start(self, env: dict[str, str]) -> None: ...

    def ensure_running(self) -> None: ...

    def stop(self) -> None: ...

    def restore(self, was_running: bool) -> None: ...


class ComposeCrawler:
    """The crawler as the compose service in docker-compose.yml.

    The settings go to compose through the environment of the commands run here, which take
    precedence over .env, so no file of the user's is ever written. Restoring is starting the
    service without them.
    """

    def __init__(self, compose_file: Path):
        self.command = ["docker", "compose", "-f", str(compose_file)]

    def _run(self, *args: str, env: dict[str, str] | None = None) -> str:
        completed = subprocess.run(
            [*self.command, *args],
            env={**os.environ, **(env or {})},
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    def is_running(self) -> bool:
        return bool(self._run("ps", "--status", "running", "-q", COMPOSE_SERVICE).strip())

    def stop_existing(self) -> None:
        self.stop()

    def start(self, env: dict[str, str]) -> None:
        self._run("up", "-d", COMPOSE_SERVICE, env=env)

    def ensure_running(self) -> None:
        if not self.is_running():
            raise RuntimeError("the crawler container stopped")

    def stop(self) -> None:
        self._run("stop", COMPOSE_SERVICE)

    def restore(self, was_running: bool) -> None:
        if was_running:
            self._run("up", "-d", COMPOSE_SERVICE)


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

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.process = None

    def restore(self, was_running: bool) -> None:
        """Nothing to restore: a running crawler is refused up front, not stopped."""


class DeviceScores:
    """Reads a device's results from the server with the crawler's own API key, so no login is needed.

    The server does the counting and gives its own clock, so a window is bounded by two server
    times and is not affected by this machine's clock being wrong.
    """

    def __init__(self, server: str, api_key: str, device_name: str):
        self.url = f"{server}/api/v1/crawler/device-results"
        self.headers = {"X-API-Key": api_key, "User-Agent": "mwmbl-benchmark"}
        self.device_name = device_name

    def read(self, since: float | None = None) -> DeviceReading:
        params = (
            {"device_name": self.device_name} if since is None else {"device_name": self.device_name, "since": since}
        )
        response = requests.get(self.url, params=params, headers=self.headers, timeout=30)
        response.raise_for_status()
        body = response.json()
        return DeviceReading(
            body["known"], body["results_since"], body["server_time"], body.get("crawler_version") or "unknown"
        )

    def check_access(self) -> None:
        self.read()


class Benchmark:
    def __init__(
        self,
        configurations: list[Configuration],
        crawler: Crawler,
        scores: DeviceScores,
        device_name: str,
        warmup_seconds: int,
        duration_seconds: int,
        csv_path: Path,
        memory_per_process_mb: int | None,
        crawler_version: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.configurations = configurations
        self.crawler = crawler
        self.scores = scores
        self.device_name = device_name
        self.warmup_seconds = warmup_seconds
        self.duration_seconds = duration_seconds
        self.csv_path = csv_path
        self.memory_per_process_mb = memory_per_process_mb
        self.crawler_version = crawler_version
        self.sleep = sleep
        self.clock = clock

    def run(self) -> list[Result]:
        # Before anything is stopped, and before a 30 minute wait finds out the key is wrong.
        self.scores.check_access()
        was_running = self.crawler.is_running()
        if was_running:
            self.crawler.stop_existing()

        results: list[Result] = []
        try:
            for number, configuration in enumerate(self.configurations, start=1):
                print(f"\n[{number}/{len(self.configurations)}] {describe(configuration)}", flush=True)
                try:
                    result = self.measure(configuration)
                except KeyboardInterrupt:
                    print("  interrupted", flush=True)
                    self.record(results, Result(configuration, STATUS_INTERRUPTED, error="interrupted"))
                    break
                self.record(results, result)
                print(f"  {result.status}: {result.index_score if result.index_score is not None else result.error}")
        finally:
            try:
                self.crawler.stop()
            finally:
                self.crawler.restore(was_running)
            print("\nRestored the original crawler configuration.", flush=True)
        return results

    def record(self, results: list[Result], result: Result) -> None:
        results.append(result)
        append_csv(self.csv_path, self.device_name, self.warmup_seconds, self.duration_seconds, result)

    def measure(self, configuration: Configuration) -> Result:
        if self.memory_per_process_mb is not None:
            needed_mb = BASE_MEMORY_MB + configuration.processes * self.memory_per_process_mb
            available_mb = total_memory_mb()
            if needed_mb > available_mb:
                error = f"needs about {needed_mb}MB, machine has {available_mb:.0f}MB"
                return Result(configuration, STATUS_SKIPPED, error=error)

        try:
            self.crawler.stop()
            self.crawler.start(
                {**configuration.env(), "MWMBL_DEVICE_NAME": self.device_name},
            )
            self.wait(self.warmup_seconds, "warming up")
            window_start = self.scores.read().server_time
            self.wait(self.duration_seconds, "measuring")
            final = self.scores.read(since=window_start)
            if not final.known:
                raise RuntimeError(
                    f"device {self.device_name!r} is not in the account's device list, so nothing was "
                    "submitted. Check MWMBL_API_KEY, CRAWL_SUBMIT_MODE and the device name."
                )
            version = self.crawler_version or final.crawler_version
            return Result(configuration, STATUS_OK, final.results_since, version)
        except Exception as error:
            # One configuration failing must not cost the results of the rest.
            return Result(configuration, STATUS_FAILED, error=f"{type(error).__name__}: {error}")
        finally:
            self.crawler.stop()

    def wait(self, seconds: int, label: str) -> None:
        deadline = self.clock() + seconds
        while (remaining := deadline - self.clock()) > 0:
            self.crawler.ensure_running()
            self.sleep(min(remaining, PROGRESS_INTERVAL_SECONDS))
            print(
                f"  {label}: {format_duration(round(max(remaining - PROGRESS_INTERVAL_SECONDS, 0)))} left", flush=True
            )


def describe(configuration: Configuration) -> str:
    return f"CRAWLER_WORKERS={configuration.processes} CRAWL_THREADS={configuration.threads}"


def append_csv(path: Path, device_name: str, warmup_seconds: int, duration_seconds: int, result: Result) -> None:
    """One row as soon as it is known, so an interrupted or crashed run loses nothing."""
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": result.finished_at.isoformat(timespec="seconds"),
                "hostname": socket.gethostname(),
                "device_name": device_name,
                "crawler_version": result.crawler_version,
                "processes": result.configuration.processes,
                "threads": result.configuration.threads,
                "warmup_seconds": warmup_seconds,
                "duration_seconds": duration_seconds,
                "index_score": "" if result.index_score is None else result.index_score,
                "status": result.status,
                "error": result.error,
            }
        )


def rank(results: list[Result]) -> list[Result]:
    """Best score first, then the cheaper configuration when scores tie, then those with no score."""

    def key(result: Result):
        has_score = result.status == STATUS_OK
        score = result.index_score or 0 if has_score else 0
        return (not has_score, -score, result.configuration.processes, result.configuration.threads)

    return sorted(results, key=key)


def format_report(results: list[Result], device_name: str, duration_seconds: int) -> str:
    lines = ["Mwmbl Crawler Benchmark", f"Device: {device_name}", f"Configurations: {len(results)}", ""]
    header = f"{'Rank':<6}{'Processes':<11}{'Threads':<9}{'Index Score':<13}{'Duration':<10}Status"
    lines += [header, "-" * len(header)]
    ranked = rank(results)
    for position, result in enumerate(ranked, start=1):
        has_score = result.status == STATUS_OK
        shown_rank = str(position) if has_score else "-"
        score = result.index_score if has_score else "-"
        duration = format_duration(duration_seconds) if has_score else "-"
        lines.append(
            f"{shown_rank:<6}{result.configuration.processes:<11}{result.configuration.threads:<9}"
            f"{score!s:<13}{duration:<10}{result.status}"
        )
    for result in ranked:
        if result.error and result.status != STATUS_INTERRUPTED:
            lines.append(f"  {describe(result.configuration)}: {result.error}")

    lines += ["", "-" * len(header)]
    winner = ranked[0] if ranked and ranked[0].status == STATUS_OK else None
    if winner is None:
        lines.append("No configuration completed, so there is no winner.")
    else:
        lines += [
            "WINNER",
            "",
            f"CRAWLER_WORKERS={winner.configuration.processes}",
            f"CRAWL_THREADS={winner.configuration.threads}",
            "",
            f"Index score: {winner.index_score}",
        ]
    lines += [
        "",
        "Index score is the number of new URLs the server added to the index from this device's",
        "submissions during the measurement window. It is one run per configuration, so close scores",
        "are within noise, and the order the configurations ran in is not randomised.",
    ]
    return "\n".join(lines)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--processes", type=parse_counts, default=DEFAULT_PROCESS_COUNTS, metavar="N,N,...")
    parser.add_argument("--threads", type=parse_counts, default=DEFAULT_THREAD_COUNTS, metavar="N,N,...")
    parser.add_argument("--warmup", type=parse_duration, default=parse_duration(DEFAULT_WARMUP), metavar="DURATION")
    parser.add_argument("--duration", type=parse_duration, default=parse_duration(DEFAULT_DURATION), metavar="DURATION")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"benchmark-results-{datetime.now(timezone.utc).date().isoformat()}.csv"),
        help="CSV to append results to (default: benchmark-results-<today>.csv)",
    )
    parser.add_argument(
        "--runner",
        choices=["compose", "process"],
        default="compose",
        help="compose: the crawler is the docker compose service, and is stopped and restarted. "
        "process: start --crawler-command as a child process; refuses to run while another crawler is.",
    )
    parser.add_argument("--compose-file", type=Path, default=Path("docker-compose.yml"))
    parser.add_argument("--crawler-command", default="mwmbl-crawl", help="Command for --runner process")
    parser.add_argument(
        "--device-name",
        default=None,
        help="Name the crawler reports, which is how its results are found. Default: MWMBL_DEVICE_NAME, "
        "else the name in ~/.mwmbl/device-name (created if missing). With --runner compose the crawler "
        "is started with this name.",
    )
    parser.add_argument("--crawler-version", default=None, help="For the CSV. Default: what the server says")
    parser.add_argument("--memory-per-process-mb", type=int, default=DEFAULT_MEMORY_PER_PROCESS_MB)
    parser.add_argument("--no-memory-check", action="store_true", help="Run combinations that look too big for RAM")


def device_name_for(args: argparse.Namespace) -> str:
    if args.device_name:
        return args.device_name
    if MWMBL_DEVICE_NAME:
        return MWMBL_DEVICE_NAME
    data_path = Path.home() / ".mwmbl"
    data_path.mkdir(exist_ok=True)
    return get_device_name(data_path)


def interrupt(signum, frame):
    raise KeyboardInterrupt


def run(args: argparse.Namespace) -> int:
    if not MWMBL_API_KEY.strip():
        raise SystemExit("MWMBL_API_KEY must be set: it is how the benchmark reads this device's results.")
    crawler: Crawler
    if args.runner == "compose":
        crawler = ComposeCrawler(args.compose_file)
    else:
        crawler = ProcessCrawler(shlex.split(args.crawler_command))
    configurations = all_configurations(args.processes, args.threads)
    device_name = device_name_for(args)
    benchmark = Benchmark(
        configurations,
        crawler,
        DeviceScores(MWMBL_REMOTE_SERVER, MWMBL_API_KEY, device_name),
        device_name,
        args.warmup,
        args.duration,
        args.output,
        None if args.no_memory_check else args.memory_per_process_mb,
        args.crawler_version,
    )
    total_seconds = len(configurations) * (args.warmup + args.duration)
    print(f"{len(configurations)} configurations on device {device_name}, about {format_duration(total_seconds)}")

    signal.signal(signal.SIGTERM, interrupt)
    results = benchmark.run()
    print("\n" + format_report(results, device_name, args.duration))
    print(f"\nResults appended to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mwmbl benchmark", description=__doc__.split("\n\n")[0])
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
