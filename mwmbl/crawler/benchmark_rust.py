"""Find the Rust crawler settings that crawl the most pages on this machine.

The Rust counterpart of benchmark.py. The Rust crawler is one process, so there is no
CRAWLER_WORKERS to tune. Its knobs are:

- CRAWL_THREADS: tokio worker threads, which run the fetches.
- PARSING_THREADS: blocking threads, used mostly for parsing HTML.
- CRAWL_MAX_TASKS: how many crawls can run at once.

The benchmark tries every combination one at a time. For each one it starts the crawler with those
settings, lets it warm up, and then measures for a fixed time. The winner is the combination that
crawled the most pages a minute. When two tie, the cheaper one wins.

The Rust crawler keeps no counter in Redis. Instead it prints a `speed:` line for every page it
crawls with content, and those lines are what get counted. Each line is a single page, so a
fixed-length window miscounts by at most one page at each end. The Python benchmark has to align
its window to whole batches because its counter only moves when a batch finishes. This one doesn't
need to, so its windows can be shorter.

Pages it couldn't fetch (robots.txt denials, errors, timeouts) print nothing, so they aren't
counted. The Python benchmark counts every fetch whatever came back, so the two numbers can't be
compared directly.

The URL queue lives in memory, so every configuration starts from a cold queue, but the bloom
filter of seen domains in ~/.mwmbl persists between runs. Later configurations therefore start
with more domains already seen than earlier ones did.

Run it from a checkout with `make optimize-rust` or `python3 -m mwmbl.crawler.benchmark_rust`.
It needs only the standard library and a built crawler:

    cargo build --release --manifest-path rust/crawler/Cargo.toml

The crawler requires MWMBL_API_KEY and MWMBL_CONTACT_INFO. Set them in the environment or in .env
(or in the file given with --env-file). Variables already in the environment take precedence.
"""

from __future__ import annotations

import argparse
import collections
import csv
import itertools
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Callable, Protocol

from mwmbl.crawler.benchmark import (
    PROGRESS_INTERVAL_SECONDS,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_OK,
    format_duration,
    interrupt,
    parse_counts,
    parse_duration,
)

# The settings tried, in the order ties are broken: the fewer threads, the cheaper.
KNOBS = ("CRAWL_THREADS", "PARSING_THREADS", "CRAWL_MAX_TASKS")
DEFAULT_COUNTS = {
    "CRAWL_THREADS": [4, 8, 16],
    "PARSING_THREADS": [4, 8],
    "CRAWL_MAX_TASKS": [64, 128, 256],
}
DEFAULT_WARMUP = "3m"
DEFAULT_DURATION = "15m"

CHECKOUT = Path(__file__).resolve().parents[2]
DEFAULT_BINARY = CHECKOUT / "rust" / "crawler" / "target" / "release" / "crawler_rs"
BUILD_COMMAND = "cargo build --release --manifest-path rust/crawler/Cargo.toml"
# What the crawler prints once for each page it crawls with content (see Crawler::run).
PAGE_LINE_PREFIX = b"speed: "
# The crawler has no SIGTERM handler and keeps nothing durable in memory, so a short grace period is enough.
STOP_TIMEOUT_SECONDS = 10
POLL_INTERVAL_SECONDS = 5
# Lines kept from the crawler's output to explain a crash. Page lines are left out: they hold only URLs.
OUTPUT_TAIL_LINES = 20
PLACEHOLDERS = {"MWMBL_API_KEY": ("", "CREATE_ME"), "MWMBL_CONTACT_INFO": ("", "CHANGE_ME@example.com")}

CSV_COLUMNS = [
    "timestamp",
    *(knob.lower() for knob in KNOBS),
    "warmup_seconds",
    "duration_seconds",
    "pages_per_minute",
    "status",
    "error",
]


@dataclass(frozen=True)
class Configuration:
    """One value for each of KNOBS, in that order."""

    values: tuple[int, ...]

    def env(self) -> dict[str, str]:
        return {knob: str(value) for knob, value in zip(KNOBS, self.values)}

    def describe(self) -> str:
        return " ".join(f"{knob}={value}" for knob, value in zip(KNOBS, self.values))


@dataclass
class Result:
    configuration: Configuration
    status: str
    pages_per_minute: float | None = None
    error: str = ""
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def all_configurations(counts: dict[str, list[int]]) -> list[Configuration]:
    return [Configuration(values) for values in itertools.product(*(counts[knob] for knob in KNOBS))]


class Crawler(Protocol):
    def start(self, env: dict[str, str]) -> None: ...

    def ensure_running(self) -> None: ...

    def pages(self) -> int:
        """How many pages the crawler started last has crawled so far."""
        ...

    def stop(self) -> None: ...


class RustCrawler:
    """The Rust crawler as a child process, in its own process group, with its output read here."""

    def __init__(self, command: list[str]):
        self.command = command
        self.process: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self.lock = threading.Lock()
        self.count = 0
        self.tail: collections.deque[str] = collections.deque(maxlen=OUTPUT_TAIL_LINES)

    def already_running(self) -> list[str]:
        """PIDs of crawlers that are already running. A second crawler would compete for the
        machine and write the same index in ~/.mwmbl, so any measurement would be meaningless."""
        if shutil.which("pgrep") is None:
            return []
        found = subprocess.run(["pgrep", "-x", Path(self.command[0]).name], capture_output=True, text=True)
        return found.stdout.split()

    def start(self, env: dict[str, str]) -> None:
        with self.lock:
            self.count = 0
            self.tail.clear()
        self.process = subprocess.Popen(
            self.command,
            env={**os.environ, **env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        stdout = self.process.stdout
        if stdout is None:
            raise RuntimeError("the crawler was started without a pipe for its output")
        self.reader = threading.Thread(target=self._read, args=(stdout,), daemon=True)
        self.reader.start()

    def _read(self, stdout: IO[bytes]) -> None:
        for line in stdout:
            with self.lock:
                if line.startswith(PAGE_LINE_PREFIX):
                    self.count += 1
                else:
                    self.tail.append(line.decode(errors="replace").rstrip())

    def pages(self) -> int:
        with self.lock:
            return self.count

    def ensure_running(self) -> None:
        if self.process is None:
            raise RuntimeError("the crawler is not started")
        exit_code = self.process.poll()
        if exit_code is not None:
            if self.reader is not None:
                self.reader.join(timeout=1)
            with self.lock:
                output = "\n".join(self.tail)
            raise RuntimeError(f"the crawler exited with code {exit_code}; its last output:\n{output}")

    def _signal_group(self, signal_number: int) -> None:
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, signal_number)
        except ProcessLookupError:
            pass

    def stop(self) -> None:
        if self.process is None:
            return
        self._signal_group(signal.SIGTERM)
        try:
            self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._signal_group(signal.SIGKILL)
            self.process.wait()
        if self.reader is not None:
            self.reader.join(timeout=STOP_TIMEOUT_SECONDS)
        self.process = None
        self.reader = None


class Benchmark:
    def __init__(
        self,
        counts: dict[str, list[int]],
        crawler: Crawler,
        warmup_seconds: int,
        duration_seconds: int,
        csv_path: Path,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.counts = counts
        self.crawler = crawler
        self.warmup_seconds = warmup_seconds
        self.duration_seconds = duration_seconds
        self.csv_path = csv_path
        self.sleep = sleep
        self.clock = clock

    def run(self) -> list[Result]:
        # Check now that the CSV path is writable, not after hours of measuring.
        self.csv_path.touch()
        results: list[Result] = []
        try:
            for number, configuration in enumerate(all_configurations(self.counts), start=1):
                print(f"\n[{number}] {configuration.describe()}", flush=True)
                try:
                    result = self.measure(configuration)
                except KeyboardInterrupt:
                    print("  interrupted", flush=True)
                    self.record(results, Result(configuration, STATUS_INTERRUPTED, error="interrupted"))
                    break
                self.record(results, result)
                shown = f"{result.pages_per_minute:.0f} pages/min" if result.status == STATUS_OK else result.error
                print(f"  {result.status}: {shown}", flush=True)
        finally:
            self.crawler.stop()
        return results

    def record(self, results: list[Result], result: Result) -> None:
        results.append(result)
        append_csv(self.csv_path, self.warmup_seconds, self.duration_seconds, result)

    def measure(self, configuration: Configuration) -> Result:
        try:
            self.crawler.start(configuration.env())
            self.wait(self.warmup_seconds, "warming up")
            return Result(configuration, STATUS_OK, self.measure_rate())
        except (RuntimeError, OSError) as error:
            # One failed configuration must not lose the results of the others.
            return Result(configuration, STATUS_FAILED, error=f"{type(error).__name__}: {error}")
        finally:
            self.crawler.stop()

    def measure_rate(self) -> float:
        opened_at, opened_pages = self.clock(), self.crawler.pages()
        self.wait(self.duration_seconds, "measuring")
        closed_at, closed_pages = self.clock(), self.crawler.pages()
        return (closed_pages - opened_pages) / (closed_at - opened_at) * 60

    def wait(self, seconds: int, label: str) -> None:
        deadline = self.clock() + seconds
        next_progress_at = self.clock() + PROGRESS_INTERVAL_SECONDS
        while (remaining := deadline - self.clock()) > 0:
            self.crawler.ensure_running()
            self.sleep(min(remaining, POLL_INTERVAL_SECONDS))
            if self.clock() >= next_progress_at:
                left = format_duration(round(max(deadline - self.clock(), 0)))
                print(f"  {label}: {left} left", flush=True)
                next_progress_at += PROGRESS_INTERVAL_SECONDS
        # Otherwise a crash during the last poll interval would be scored as a slow configuration.
        self.crawler.ensure_running()


def append_csv(path: Path, warmup_seconds: int, duration_seconds: int, result: Result) -> None:
    """Write each row as soon as it's known, so an interrupted or crashed run loses nothing."""
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": result.finished_at.isoformat(timespec="seconds"),
                **{knob.lower(): value for knob, value in zip(KNOBS, result.configuration.values)},
                "warmup_seconds": warmup_seconds,
                "duration_seconds": duration_seconds,
                "pages_per_minute": "" if result.pages_per_minute is None else round(result.pages_per_minute, 1),
                "status": result.status,
                "error": result.error,
            }
        )


def rank(results: list[Result]) -> list[Result]:
    """Best rate first, then the cheaper configuration on a tie, then those that have no rate."""

    def key(result: Result):
        has_rate = result.status == STATUS_OK
        rate = (result.pages_per_minute or 0) if has_rate else 0
        return (not has_rate, -rate, result.configuration.values)

    return sorted(results, key=key)


def format_report(results: list[Result]) -> str:
    widths = [len(knob) + 2 for knob in KNOBS]
    header = f"{'Rank':<6}" + "".join(f"{knob:<{width}}" for knob, width in zip(KNOBS, widths))
    header += f"{'Pages/min':<11}Status"
    lines = ["Mwmbl Rust Crawler Benchmark", f"Configurations: {len(results)}", "", header, "-" * len(header)]
    ranked = rank(results)
    for position, result in enumerate(ranked, start=1):
        has_rate = result.status == STATUS_OK
        row = f"{position if has_rate else '-':<6}"
        row += "".join(f"{value:<{width}}" for value, width in zip(result.configuration.values, widths))
        row += f"{f'{result.pages_per_minute:.0f}' if has_rate else '-':<11}{result.status}"
        lines.append(row)
    for result in ranked:
        if result.error and result.status != STATUS_INTERRUPTED:
            lines.append(f"  {result.configuration.describe()}: {result.error}")

    lines += ["", "-" * len(header)]
    winner = ranked[0] if ranked and ranked[0].status == STATUS_OK else None
    if winner is None:
        lines.append("No configuration completed, so there is no winner.")
    else:
        lines += ["WINNER", "", *winner.configuration.describe().split(), ""]
        lines.append(f"Pages per minute: {winner.pages_per_minute:.0f}")
    lines += [
        "",
        "Pages per minute counts the pages the crawler crawled with content. Each configuration gets",
        "one short run, so rates within about 10% of each other are within noise.",
    ]
    return "\n".join(lines)


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, as in a Compose .env. Comments, blank lines and an `export ` prefix are
    allowed, and quotes around a value are removed."""
    if not path.exists():
        return {}
    loaded = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[name.strip()] = value
    return loaded


def check_settings(env: dict[str, str]) -> None:
    unset = [name for name, placeholders in PLACEHOLDERS.items() if env.get(name, "").strip() in placeholders]
    if unset:
        raise SystemExit(
            f"Set {', '.join(unset)} in your environment or .env before benchmarking: the crawler won't start "
            "without them. Use --env-file to choose another settings file."
        )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    for knob in KNOBS:
        parser.add_argument(
            f"--{knob.lower().replace('_', '-')}",
            dest=knob,
            type=parse_counts,
            default=DEFAULT_COUNTS[knob],
            metavar="N,N,...",
            help=f"{knob} values to try (default: {','.join(map(str, DEFAULT_COUNTS[knob]))})",
        )
    parser.add_argument("--warmup", type=parse_duration, default=parse_duration(DEFAULT_WARMUP), metavar="DURATION")
    parser.add_argument("--duration", type=parse_duration, default=parse_duration(DEFAULT_DURATION), metavar="DURATION")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"benchmark-results-rust-{datetime.now(timezone.utc).date().isoformat()}.csv"),
        help="CSV to append results to (default: benchmark-results-rust-<today>.csv)",
    )
    parser.add_argument(
        "--crawler-command",
        default=None,
        help="Command that starts the crawler (default: rust/crawler/target/release/crawler_rs in the checkout)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Settings loaded into the crawler's environment; variables already set take precedence (default: .env)",
    )


def run(args: argparse.Namespace) -> int:
    command = [str(DEFAULT_BINARY)] if args.crawler_command is None else shlex.split(args.crawler_command)
    if args.crawler_command is None and not DEFAULT_BINARY.exists():
        raise SystemExit(f"There is no crawler at {DEFAULT_BINARY}. Build it with `{BUILD_COMMAND}`.")
    for name, value in load_env_file(args.env_file).items():
        os.environ.setdefault(name, value)
    check_settings(dict(os.environ))

    crawler = RustCrawler(command)
    if pids := crawler.already_running():
        raise SystemExit(f"A crawler is already running (pid {', '.join(pids)}). Stop it first.")

    counts = {knob: getattr(args, knob) for knob in KNOBS}
    total = len(all_configurations(counts))
    each = args.warmup + args.duration
    print(
        f"{total} configurations of {format_duration(each)} each: about {format_duration(total * each)} in all",
        flush=True,
    )

    signal.signal(signal.SIGTERM, interrupt)
    results = Benchmark(counts, crawler, args.warmup, args.duration, args.output).run()
    print("\n" + format_report(results))
    print(f"\nResults appended to {args.output}")
    return 0 if any(result.status == STATUS_OK for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark_rust", description=__doc__.split("\n\n")[0])
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
