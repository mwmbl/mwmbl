"""Find the crawler configuration that fetches the most pages on this machine.

Tries combinations of process count and thread count one at a time: stop the crawler, start it
with that combination, let it warm up, then measure for a fixed time. The winner is the
combination that fetched the most pages a minute.

What is measured is the crawler's own count of fetches, `crawl-domain-overlap fetches` in its
Redis, which every worker adds its batch to as the batch finishes. Nothing is read from the
Mwmbl server: submissions reach it minutes after the fetches that produced them, so measuring
what it indexed needs windows of half an hour, where fetch throughput settles in seconds.

Batches arrive in bursts, so the window opens at the first batch to finish after warmup and
closes at the first one to finish after the duration has passed. Both ends are then exact
counts of whole batches, not a fixed time that cuts through them.

The search stops climbing wherever more threads, or more processes, no longer fetch noticeably
more, since the winner is always the cheapest configuration that is close to the best.
`--exhaustive` tries every combination instead.

Nothing here says "Python": it needs only a way to start and stop the crawler with CRAWLER_WORKERS
and CRAWL_THREADS set, and to read its Redis.

Run as `make optimize`, `mwmbl benchmark` or `python -m mwmbl.crawler.benchmark`.
For the default Compose crawler, set MWMBL_API_KEY and MWMBL_CONTACT_INFO in .env or
the environment before running the benchmark.

The Compose runner needs nothing but Docker and the standard library of Python 3.9 or later, so
this file runs on its own, with no checkout or install: deploy/crawler/benchmark.sh fetches it.
"""

# Annotations like `int | None` would otherwise need Python 3.10, and macOS ships 3.9.
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Protocol

DEFAULT_PROCESS_COUNTS = [1, 2, 3, 4, 5]
DEFAULT_THREAD_COUNTS = [10, 25, 50, 100]
DEFAULT_WARMUP = "10s"
DEFAULT_DURATION = "30s"

# How much more a configuration has to fetch than the one before it to be worth climbing past.
# Below this, two configurations are within the noise of a run this short, and the cheaper wins.
MINIMUM_GAIN = 0.10

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_INTERRUPTED = "INTERRUPTED"

COMPOSE_SERVICE = "mwmbl-crawler"
REDIS_SERVICE = "redis_crawler"
FETCHES_KEY = "crawl-domain-overlap"
FETCHES_FIELD = "fetches"
# What the benchmark overrides, so what restoring the user's crawler has to put back.
RESTORED_SETTINGS = ("CRAWLER_WORKERS", "CRAWL_THREADS")
# The crawler is PID 1 in its container and has no SIGTERM handler, so the default ten seconds
# of grace is always spent waiting for a signal that is ignored. Everything durable is in Redis.
CONTAINER_STOP_GRACE_SECONDS = 2
PROGRESS_INTERVAL_SECONDS = 10
POLL_INTERVAL_SECONDS = 1
# A crawler that has finished no batch in this many durations is not slow, it is stuck.
GIVE_UP_AFTER_DURATIONS = 6

# Rough, and only used to skip combinations that would obviously exhaust memory: the indexing
# process and Redis, then each crawl worker. Memory scales with processes, not threads. The
# throughput still decides the winner of every combination that is run.
BASE_MEMORY_MB = 600
DEFAULT_MEMORY_PER_PROCESS_MB = 400

CSV_COLUMNS = [
    "timestamp",
    "processes",
    "threads",
    "warmup_seconds",
    "duration_seconds",
    "fetches_per_minute",
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
    fetches_per_minute: float | None = None
    error: str = ""
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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


def search(
    process_counts: list[int], thread_counts: list[int], rates: dict[Configuration, float], exhaustive: bool
) -> Iterator[Configuration]:
    """The configurations to try, cheapest first, reading each one's rate from `rates` once it has run.

    Within a process count the threads climb until a step gains less than MINIMUM_GAIN. The process
    counts climb until the first thread count of one gains less than that over the same thread count
    with the process count before: doubling the concurrency fetched no more, so the machine, the
    network or the sites are the limit. Configurations that fail or are skipped have no rate and
    do not end a climb.
    """
    thread_floor = thread_counts[0]
    previous_processes = None
    for processes in process_counts:
        previous_rate = None
        for threads in thread_counts:
            configuration = Configuration(processes, threads)
            yield configuration
            rate = rates.get(configuration)
            if rate is None:
                continue
            if not exhaustive and previous_rate is not None and rate < previous_rate * (1 + MINIMUM_GAIN):
                break
            previous_rate = rate
            if exhaustive or threads != thread_floor or previous_processes is None:
                continue
            fewer_processes_rate = rates.get(Configuration(previous_processes, threads))
            if fewer_processes_rate is not None and rate < fewer_processes_rate * (1 + MINIMUM_GAIN):
                return
        previous_processes = processes


class Crawler(Protocol):
    def is_running(self) -> bool: ...

    def stop_existing(self) -> None: ...

    def start(self, env: dict[str, str]) -> None: ...

    def ensure_running(self) -> None: ...

    def fetches(self) -> int:
        """How many URLs the crawler has finished fetching, in total, as of its last finished batch."""
        ...

    def stop(self) -> None: ...

    def restore(self, was_running: bool) -> None: ...


class ComposeCrawler:
    """The crawler as the compose service in docker-compose.yml.

    The settings go to compose through the environment of the commands run here, which take
    precedence over .env, so no file of the user's is ever written. Restoring starts the service
    with the settings the container it replaced was running with. `--pull never` throughout, since
    the service's `pull_policy: always` would otherwise swap the crawler version under a run.
    """

    def __init__(self, compose_file: Path, env_file: Path | None = None):
        env_file_option = [] if env_file is None else ["--env-file", str(env_file)]
        self.command = ["docker", "compose", "-f", str(compose_file), *env_file_option]
        self.original_env: dict[str, str] = {}

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

    def validate(self) -> None:
        config = json.loads(self._run("config", "--format", "json", COMPOSE_SERVICE))
        environment = config["services"][COMPOSE_SERVICE].get("environment", {})
        problems = []
        api_key = environment.get("MWMBL_API_KEY")
        if (
            environment.get("CRAWL_SUBMIT_MODE") != "off"
            and "MWMBL_API_KEY" in environment
            and (not api_key or api_key.strip() in ("", "CREATE_ME"))
        ):
            problems.append("MWMBL_API_KEY")
        contact = environment.get("MWMBL_CONTACT_INFO")
        if "MWMBL_CONTACT_INFO" in environment and (not contact or contact.strip() in ("", "CHANGE_ME@example.com")):
            problems.append("MWMBL_CONTACT_INFO")
        if problems:
            raise SystemExit(
                f"Set {', '.join(problems)} in your environment or .env before benchmarking the crawler. "
                "Use --env-file to select another settings file."
            )

    def stop_existing(self) -> None:
        """Stop the user's crawler, first noting the settings it runs with: they may have come from
        the shell that started it, and then nothing in .env or here would bring them back."""
        container = self._run("ps", "-q", COMPOSE_SERVICE).strip()
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", container],
            check=True,
            capture_output=True,
            text=True,
        )
        assignments = [line.split("=", 1) for line in json.loads(inspected.stdout)]
        self.original_env = {name: value for name, value in assignments if name in RESTORED_SETTINGS}
        self.stop()

    def start(self, env: dict[str, str]) -> None:
        self._run("up", "-d", "--pull", "never", COMPOSE_SERVICE, env=env)

    def ensure_running(self) -> None:
        if not self.is_running():
            logs = shlex.join([*self.command, "logs", "--tail", "100", COMPOSE_SERVICE])
            raise RuntimeError(f"the crawler container stopped; inspect its logs with: {logs}")

    def fetches(self) -> int:
        counted = self._run("exec", "-T", REDIS_SERVICE, "redis-cli", "HGET", FETCHES_KEY, FETCHES_FIELD)
        return int(counted.strip() or 0)

    def stop(self) -> None:
        self._run("stop", "--timeout", str(CONTAINER_STOP_GRACE_SECONDS), COMPOSE_SERVICE)

    def restore(self, was_running: bool) -> None:
        if was_running:
            self.start(self.original_env)


class Benchmark:
    def __init__(
        self,
        process_counts: list[int],
        thread_counts: list[int],
        exhaustive: bool,
        crawler: Crawler,
        warmup_seconds: int,
        duration_seconds: int,
        csv_path: Path,
        memory_per_process_mb: int | None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.process_counts = process_counts
        self.thread_counts = thread_counts
        self.exhaustive = exhaustive
        self.crawler = crawler
        self.warmup_seconds = warmup_seconds
        self.duration_seconds = duration_seconds
        self.csv_path = csv_path
        self.memory_per_process_mb = memory_per_process_mb
        self.sleep = sleep
        self.clock = clock

    def run(self) -> list[Result]:
        # Before anything is stopped, so that an unwritable CSV path is found out now and not
        # after the user's crawler is down.
        self.csv_path.touch()
        was_running = self.crawler.is_running()

        results: list[Result] = []
        rates: dict[Configuration, float] = {}
        try:
            if was_running:
                self.crawler.stop_existing()
            plan = search(self.process_counts, self.thread_counts, rates, self.exhaustive)
            for number, configuration in enumerate(plan, start=1):
                print(f"\n[{number}] {describe(configuration)}", flush=True)
                try:
                    result = self.measure(configuration)
                except KeyboardInterrupt:
                    print("  interrupted", flush=True)
                    self.record(results, Result(configuration, STATUS_INTERRUPTED, error="interrupted"))
                    break
                self.record(results, result)
                if result.fetches_per_minute is not None:
                    rates[configuration] = result.fetches_per_minute
                shown = f"{result.fetches_per_minute:.0f} fetches/min" if result.fetches_per_minute else result.error
                print(f"  {result.status}: {shown}")
        finally:
            try:
                self.crawler.stop()
            finally:
                self.crawler.restore(was_running)
            if was_running:
                print("\nRestored the original crawler.", flush=True)
        return results

    def record(self, results: list[Result], result: Result) -> None:
        results.append(result)
        append_csv(self.csv_path, self.warmup_seconds, self.duration_seconds, result)

    def measure(self, configuration: Configuration) -> Result:
        if self.memory_per_process_mb is not None:
            needed_mb = BASE_MEMORY_MB + configuration.processes * self.memory_per_process_mb
            available_mb = total_memory_mb()
            if needed_mb > available_mb:
                error = f"needs about {needed_mb}MB, machine has {available_mb:.0f}MB"
                return Result(configuration, STATUS_SKIPPED, error=error)

        try:
            self.crawler.stop()
            self.crawler.start(configuration.env())
            self.wait(self.warmup_seconds, "warming up")
            rate = self.measure_rate()
            return Result(configuration, STATUS_OK, rate)
        except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
            # One configuration failing must not cost the results of the rest.
            return Result(configuration, STATUS_FAILED, error=f"{type(error).__name__}: {error}")
        finally:
            self.crawler.stop()

    def measure_rate(self) -> float:
        """Fetches a minute between two batches finishing, at least the duration apart.

        The counter only moves when a batch finishes, so a window of fixed length would start
        and end part way through batches and miscount them by up to a batch each end, which for
        one process is most of what it fetched. The count and the time between two changes of
        the counter are both exact, to the poll interval.
        """
        started_at = self.clock()
        give_up_at = started_at + GIVE_UP_AFTER_DURATIONS * self.duration_seconds
        last_seen = self.crawler.fetches()
        opened: tuple[float, int] | None = None
        window_end = 0.0
        while self.clock() < give_up_at:
            self.crawler.ensure_running()
            self.sleep(POLL_INTERVAL_SECONDS)
            now = self.clock()
            fetches = self.crawler.fetches()
            if fetches == last_seen:
                continue
            last_seen = fetches
            if opened is None:
                opened = (now, fetches)
                window_end = now + self.duration_seconds
                print("  measuring", flush=True)
            elif now >= window_end:
                opened_at, opened_fetches = opened
                return (fetches - opened_fetches) / (now - opened_at) * 60
        raise RuntimeError(
            f"no window of {format_duration(self.duration_seconds)} closed in time: batches are too slow"
        )

    def wait(self, seconds: int, label: str) -> None:
        deadline = self.clock() + seconds
        while (remaining := deadline - self.clock()) > 0:
            self.crawler.ensure_running()
            self.sleep(min(remaining, PROGRESS_INTERVAL_SECONDS))
            print(
                f"  {label}: {format_duration(round(max(remaining - PROGRESS_INTERVAL_SECONDS, 0)))} left", flush=True
            )
        # A crash in the last interval would otherwise be scored as a slow configuration.
        self.crawler.ensure_running()


def describe(configuration: Configuration) -> str:
    return f"CRAWLER_WORKERS={configuration.processes} CRAWL_THREADS={configuration.threads}"


def append_csv(path: Path, warmup_seconds: int, duration_seconds: int, result: Result) -> None:
    """One row as soon as it is known, so an interrupted or crashed run loses nothing."""
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": result.finished_at.isoformat(timespec="seconds"),
                "processes": result.configuration.processes,
                "threads": result.configuration.threads,
                "warmup_seconds": warmup_seconds,
                "duration_seconds": duration_seconds,
                "fetches_per_minute": "" if result.fetches_per_minute is None else round(result.fetches_per_minute, 1),
                "status": result.status,
                "error": result.error,
            }
        )


def rank(results: list[Result]) -> list[Result]:
    """Best rate first, then the cheaper configuration when rates tie, then those with no rate."""

    def key(result: Result):
        has_rate = result.status == STATUS_OK
        rate = result.fetches_per_minute or 0 if has_rate else 0
        return (not has_rate, -rate, result.configuration.processes, result.configuration.threads)

    return sorted(results, key=key)


def format_report(results: list[Result]) -> str:
    lines = ["Mwmbl Crawler Benchmark", f"Configurations: {len(results)}", ""]
    header = f"{'Rank':<6}{'Processes':<11}{'Threads':<9}{'Fetches/min':<13}Status"
    lines += [header, "-" * len(header)]
    ranked = rank(results)
    for position, result in enumerate(ranked, start=1):
        has_rate = result.status == STATUS_OK
        shown_rank = str(position) if has_rate else "-"
        rate = f"{result.fetches_per_minute:.0f}" if has_rate else "-"
        lines.append(
            f"{shown_rank:<6}{result.configuration.processes:<11}{result.configuration.threads:<9}"
            f"{rate:<13}{result.status}"
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
            f"Fetches per minute: {winner.fetches_per_minute:.0f}",
        ]
    lines += [
        "",
        "Fetches per minute is the URLs the crawler finished fetching, whatever came back. It is one",
        "short run per configuration, so rates within about 10% are within noise, and more threads",
        "always fetch more up to what the machine and the sites crawled can bear.",
    ]
    return "\n".join(lines)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--processes", type=parse_counts, default=DEFAULT_PROCESS_COUNTS, metavar="N,N,...")
    parser.add_argument("--threads", type=parse_counts, default=DEFAULT_THREAD_COUNTS, metavar="N,N,...")
    parser.add_argument("--warmup", type=parse_duration, default=parse_duration(DEFAULT_WARMUP), metavar="DURATION")
    parser.add_argument(
        "--duration",
        type=parse_duration,
        default=parse_duration(DEFAULT_DURATION),
        metavar="DURATION",
        help="At least this long between the batch that opens the measurement and the one that closes it",
    )
    parser.add_argument(
        "--exhaustive",
        action="store_true",
        help="Try every combination, instead of stopping a climb once more threads or processes stop helping",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"benchmark-results-fetches-{datetime.now(timezone.utc).date().isoformat()}.csv"),
        help="CSV to append results to (default: benchmark-results-fetches-<today>.csv)",
    )
    parser.add_argument(
        "--runner",
        choices=["compose", "process"],
        default="compose",
        help="compose: the crawler is the docker compose service, and is stopped and restarted. "
        "process: start --crawler-command as a child process; refuses to run while another crawler is.",
    )
    parser.add_argument("--compose-file", type=Path, default=Path("docker-compose.yml"))
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Settings file. With --runner compose, docker compose reads it instead of the .env beside "
        "--compose-file. With --runner process it is loaded into the environment the crawler inherits "
        "(default: .env beside --compose-file), and variables already in the environment win.",
    )
    parser.add_argument("--crawler-command", default="mwmbl-crawl", help="Command for --runner process")
    parser.add_argument("--memory-per-process-mb", type=int, default=DEFAULT_MEMORY_PER_PROCESS_MB)
    parser.add_argument("--no-memory-check", action="store_true", help="Run combinations that look too big for RAM")


def interrupt(signum, frame):
    raise KeyboardInterrupt


def run(args: argparse.Namespace) -> int:
    crawler: Crawler
    if args.runner == "compose":
        if not args.compose_file.exists():
            raise SystemExit(
                f"There is no {args.compose_file} here. Run the benchmark from the directory you start your "
                "crawler from, or pass --compose-file."
            )
        compose = ComposeCrawler(args.compose_file, args.env_file)
        compose.validate()
        crawler = compose
    else:
        # Imported only here: it needs psutil, redis and python-dotenv, and the compose runner nothing.
        from mwmbl.crawler.benchmark_process import process_crawler

        env_file = args.env_file or args.compose_file.parent / ".env"
        crawler = process_crawler(shlex.split(args.crawler_command), env_file)
    benchmark = Benchmark(
        args.processes,
        args.threads,
        args.exhaustive,
        crawler,
        args.warmup,
        args.duration,
        args.output,
        None if args.no_memory_check else args.memory_per_process_mb,
    )
    most = len(all_configurations(args.processes, args.threads))
    print(f"Up to {most} configurations, each about {format_duration(args.warmup + args.duration)} or more")

    signal.signal(signal.SIGTERM, interrupt)
    results = benchmark.run()
    print("\n" + format_report(results))
    print(f"\nResults appended to {args.output}")
    return 0 if any(result.status == STATUS_OK for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mwmbl benchmark", description=__doc__.split("\n\n")[0])
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
