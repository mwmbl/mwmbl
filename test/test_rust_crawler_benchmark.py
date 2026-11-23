"""The Rust crawler benchmark: which configurations it runs, how it measures them, and what it reports."""

import argparse
import csv
import sys
import time

import pytest

from mwmbl.crawler import benchmark_rust
from mwmbl.crawler.benchmark_rust import (
    Benchmark,
    Configuration,
    Result,
    RustCrawler,
    all_configurations,
    check_settings,
    format_duration,
    format_report,
    load_env_file,
    parse_counts,
    parse_duration,
    rank,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def rate(env):
    """Pages a second: scales with threads and tasks, so the biggest configuration wins."""
    return int(env["CRAWL_THREADS"]) * int(env["CRAWL_MAX_TASKS"]) / 100


class FakeCrawler:
    def __init__(self, clock, rate=rate, fail_start_for=None, die_at=None):
        self.clock = clock
        self.rate = rate
        self.fail_start_for = fail_start_for
        self.die_at = die_at
        self.env = None
        self.started_at = 0.0
        self.events = []

    def start(self, env):
        self.events.append(("start", env))
        if env == self.fail_start_for:
            raise RuntimeError("could not start")
        self.env = env
        self.started_at = self.clock.now

    def ensure_running(self):
        if self.env is None:
            raise RuntimeError("not started")
        if self.die_at is not None and self.clock.now >= self.die_at:
            raise RuntimeError("the crawler exited with code 1")

    def pages(self):
        return int((self.clock.now - self.started_at) * self.rate(self.env)) if self.env else 0

    def stop(self):
        self.events.append(("stop",))
        self.env = None


def counts(threads=(1, 2), blocking=(4,), tasks=(64, 128)):
    return {"CRAWL_THREADS": list(threads), "BLOCKING_THREADS": list(blocking), "CRAWL_MAX_TASKS": list(tasks)}


def run_benchmark(tmp_path, crawler, clock, grid=None):
    bench = Benchmark(grid or counts(), crawler, 60, 600, tmp_path / "out.csv", sleep=clock.sleep, clock=clock)
    return bench.run()


def test_tries_every_combination(tmp_path):
    clock = Clock()
    crawler = FakeCrawler(clock)
    results = run_benchmark(tmp_path, crawler, clock)
    started = [event[1] for event in crawler.events if event[0] == "start"]
    assert started == [configuration.env() for configuration in all_configurations(counts())]
    assert len(results) == 4
    assert all(result.status == "OK" for result in results)


def test_measures_pages_a_minute_after_warmup(tmp_path):
    clock = Clock()
    results = run_benchmark(tmp_path, FakeCrawler(clock), clock, counts(threads=[2], tasks=[100]))
    (result,) = results
    assert result.pages_per_minute == pytest.approx(2 * 100 / 100 * 60)


def test_a_failing_configuration_does_not_lose_the_others(tmp_path):
    clock = Clock()
    failing = Configuration((1, 4, 128)).env()
    crawler = FakeCrawler(clock, fail_start_for=failing)
    results = run_benchmark(tmp_path, crawler, clock)
    statuses = {result.configuration.values: result.status for result in results}
    assert statuses[(1, 4, 128)] == "FAILED"
    assert [status for status in statuses.values()].count("OK") == 3


def test_a_crash_late_in_the_window_fails_the_configuration(tmp_path):
    clock = Clock()
    # The window runs from 60s to 660s: this dies during its last poll.
    crawler = FakeCrawler(clock, die_at=658)
    (result,) = run_benchmark(tmp_path, crawler, clock, counts(threads=[1], tasks=[64]))
    assert result.status == "FAILED"
    assert "exited" in result.error


def test_stops_the_crawler_after_every_configuration(tmp_path):
    clock = Clock()
    crawler = FakeCrawler(clock)
    run_benchmark(tmp_path, crawler, clock)
    kinds = [event[0] for event in crawler.events]
    assert kinds.count("start") == 4
    assert kinds[-1] == "stop"
    for index, kind in enumerate(kinds):
        if kind == "start":
            assert kinds[index + 1] == "stop"


def test_interrupt_records_what_finished_and_stops_the_crawler(tmp_path):
    clock = Clock()

    class Interrupting(FakeCrawler):
        def start(self, env):
            if len([e for e in self.events if e[0] == "start"]) == 2:
                raise KeyboardInterrupt
            super().start(env)

    crawler = Interrupting(clock)
    results = run_benchmark(tmp_path, crawler, clock)
    assert [result.status for result in results] == ["OK", "OK", "INTERRUPTED"]
    assert crawler.env is None
    with (tmp_path / "out.csv").open() as file:
        rows = list(csv.DictReader(file))
    assert [row["status"] for row in rows] == ["OK", "OK", "INTERRUPTED"]
    assert rows[0]["crawl_threads"] == "1"
    assert rows[0]["blocking_threads"] == "4"
    assert rows[0]["crawl_max_tasks"] == "64"


def test_ties_go_to_the_cheaper_configuration():
    results = [
        Result(Configuration((8, 4, 64)), "OK", 100.0),
        Result(Configuration((4, 4, 64)), "OK", 100.0),
        Result(Configuration((16, 8, 256)), "FAILED", error="boom"),
        Result(Configuration((2, 4, 64)), "OK", 50.0),
    ]
    assert [result.configuration.values for result in rank(results)] == [
        (4, 4, 64),
        (8, 4, 64),
        (2, 4, 64),
        (16, 8, 256),
    ]


def test_report_names_the_winner():
    report = format_report([Result(Configuration((8, 4, 128)), "OK", 1234.0)])
    assert "CRAWL_THREADS=8\nBLOCKING_THREADS=4\nCRAWL_MAX_TASKS=128" in report
    assert "Pages per minute: 1234" in report


def test_report_without_a_winner():
    report = format_report([Result(Configuration((8, 4, 128)), "FAILED", error="boom")])
    assert "no winner" in report
    assert "boom" in report


def test_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\n\nMWMBL_API_KEY='abc'\nexport MWMBL_CONTACT_INFO=\"me@example.org\"\nJUNK\n")
    assert load_env_file(path) == {"MWMBL_API_KEY": "abc", "MWMBL_CONTACT_INFO": "me@example.org"}
    assert load_env_file(tmp_path / "missing") == {}


def test_refuses_placeholder_settings():
    with pytest.raises(SystemExit, match="MWMBL_API_KEY"):
        check_settings({"MWMBL_API_KEY": "CREATE_ME", "MWMBL_CONTACT_INFO": "me@example.org"})
    check_settings({"MWMBL_API_KEY": "abc", "MWMBL_CONTACT_INFO": "me@example.org"})


FAKE_CRAWLER = """
import os, sys, time
print("CrawlerParams { api_key: " + repr(os.environ.get("MWMBL_API_KEY")) + " }", flush=True)
print("Sending results.", flush=True)
for i in range(int(os.environ["CRAWL_THREADS"])):
    print(f"speed: 1/s | queue https://example.org/{i} | crawled 0", flush=True)
if os.environ.get("CRASH"):
    print("thread 'main' panicked with key " + os.environ.get("MWMBL_API_KEY", ""), flush=True)
    sys.exit(3)
time.sleep(60)
"""


def wait_for_pages(crawler, expected):
    for _ in range(200):
        if crawler.pages() == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"saw {crawler.pages()} pages, expected {expected}")


def test_rust_crawler_counts_page_lines_and_stops():
    crawler = RustCrawler([sys.executable, "-c", FAKE_CRAWLER])
    crawler.start({"CRAWL_THREADS": "5"})
    try:
        wait_for_pages(crawler, 5)
        crawler.ensure_running()
    finally:
        crawler.stop()
    assert crawler.process is None
    # A fresh start counts from zero.
    crawler.start({"CRAWL_THREADS": "2"})
    try:
        wait_for_pages(crawler, 2)
    finally:
        crawler.stop()


def test_rust_crawler_reports_a_crash_with_its_output():
    crawler = RustCrawler([sys.executable, "-c", FAKE_CRAWLER])
    crawler.start({"CRAWL_THREADS": "1", "CRASH": "1", "MWMBL_API_KEY": "secret-key"})
    process = crawler.process
    assert process is not None
    try:
        process.wait(timeout=10)
        with pytest.raises(RuntimeError, match="code 3") as raised:
            crawler.ensure_running()
    finally:
        crawler.stop()
    assert "panicked" in str(raised.value)
    assert "example.org" not in str(raised.value)
    assert "secret-key" not in str(raised.value)
    assert "CrawlerParams" not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_refuses_an_empty_window(tmp_path):
    with pytest.raises(SystemExit):
        benchmark_rust.main(["--duration", "0s", "--env-file", str(tmp_path / ".env")])


def test_cli_refuses_to_run_without_a_built_crawler(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark_rust, "DEFAULT_BINARY", tmp_path / "missing")
    with pytest.raises(SystemExit, match="cargo build"):
        benchmark_rust.main(["--env-file", str(tmp_path / ".env")])


def test_parse_counts():
    assert parse_counts("8,16,8") == [8, 16]
    for bad in ("", "0", "a,1", "-1"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_counts(bad)


def test_durations():
    assert [parse_duration(text) for text in ("90s", "5m", "1h")] == [90, 300, 3600]
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration("5")
    assert [format_duration(seconds) for seconds in (0, 45, 89, 1800, 3753)] == [
        "0s",
        "45s",
        "1m 29s",
        "30m",
        "1h 2m 33s",
    ]
