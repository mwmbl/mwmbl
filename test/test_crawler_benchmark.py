"""The crawler benchmark: which configurations it runs, how it measures them and what it leaves behind."""

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
from functools import partial
from pathlib import Path

import psutil
import pytest

from mwmbl import cli
from mwmbl.crawler import benchmark, benchmark_process
from mwmbl.crawler.benchmark import (
    Benchmark,
    ComposeCrawler,
    Configuration,
    Result,
    all_configurations,
    format_report,
    parse_counts,
    parse_duration,
)
from mwmbl.crawler.benchmark_process import ProcessCrawler

# The fake crawler's batches finish together, every this many seconds.
BATCH_SECONDS = 10


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def linear_rate(env):
    """Fetches a minute: one thread fetches six pages a minute."""
    return int(env["CRAWLER_WORKERS"]) * int(env["CRAWL_THREADS"]) * 6


class FakeCrawler:
    """Fetches at the rate it is given for the configuration it was started with, in bursts like batches
    finishing, and fails the way a real crawler could be made to."""

    def __init__(
        self,
        clock,
        rate=linear_rate,
        running_at_start=False,
        fail_start_for=None,
        die_after_checks=None,
        stop_existing_error=None,
    ):
        self.clock = clock
        self.rate = rate
        self.running = running_at_start
        self.fail_start_for = fail_start_for
        self.die_after_checks = die_after_checks
        self.stop_existing_error = stop_existing_error
        self.checks = 0
        self.events: list[tuple] = []
        self.started_while_running = False
        self.started_at = 0.0
        self.env: dict[str, str] = {}
        # A real counter lives in Redis, so it outlasts the crawler that was counting.
        self.banked = 0

    def produced(self):
        if not self.running or not self.env:
            return 0
        bursts = int((self.clock.now - self.started_at) // BATCH_SECONDS)
        return round(bursts * self.rate(self.env) * BATCH_SECONDS / 60)

    def fetches(self):
        return self.banked + self.produced()

    def is_running(self):
        return self.running

    def validate(self):
        pass

    def stop_existing(self):
        if self.stop_existing_error:
            raise self.stop_existing_error
        self.stop()

    def start(self, env):
        if self.running:
            self.started_while_running = True
        if env["CRAWL_THREADS"] == self.fail_start_for:
            raise RuntimeError("cannot start")
        self.running = True
        self.env = env
        self.started_at = self.clock.now
        self.events.append(("start", env))

    def ensure_running(self):
        self.checks += 1
        if self.die_after_checks is not None and self.checks > self.die_after_checks:
            raise RuntimeError("the crawler died")

    def stop(self):
        self.banked += self.produced()
        self.running = False
        self.events.append(("stop",))

    def restore(self, was_running):
        self.events.append(("restore", was_running))


def make_benchmark(tmp_path, crawler, process_counts=(1, 2), thread_counts=(10, 25), **overrides):
    arguments = {
        "process_counts": list(process_counts),
        "thread_counts": list(thread_counts),
        "crawler": crawler,
        "warmup_seconds": 20,
        "duration_seconds": 60,
        "csv_path": tmp_path / "results.csv",
        "memory_per_process_mb": None,
        "sleep": crawler.clock.sleep,
        "clock": crawler.clock,
        **overrides,
    }
    return Benchmark(**arguments)


def starts(crawler):
    return [event[1] for event in crawler.events if event[0] == "start"]


def test_the_default_search_space_is_all_twenty_combinations():
    configurations = all_configurations(benchmark.DEFAULT_PROCESS_COUNTS, benchmark.DEFAULT_THREAD_COUNTS)

    assert len(configurations) == 20
    assert len(set(configurations)) == 20
    assert Configuration(1, 10) in configurations
    assert Configuration(5, 100) in configurations


def test_a_custom_search_space_is_exactly_what_was_asked_for():
    processes = parse_counts("1,3,3")
    threads = parse_counts("8,16")

    assert all_configurations(processes, threads) == [
        Configuration(1, 8),
        Configuration(1, 16),
        Configuration(3, 8),
        Configuration(3, 16),
    ]


@pytest.mark.parametrize("text", ["0", "2,-1", "a", ""])
def test_counts_must_be_positive_integers(text):
    with pytest.raises((argparse.ArgumentTypeError, ValueError)):
        parse_counts(text)


def test_durations_are_shown_in_hours_minutes_and_seconds_without_the_zero_ones():
    assert benchmark.format_duration(0) == "0s"
    assert benchmark.format_duration(30) == "30s"
    assert benchmark.format_duration(89) == "1m 29s"
    assert benchmark.format_duration(1800) == "30m"
    assert benchmark.format_duration(1729) == "28m 49s"
    assert benchmark.format_duration(3600) == "1h"
    assert benchmark.format_duration(3753) == "1h 2m 33s"


def test_durations_have_a_unit():
    assert parse_duration("90s") == 90
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration("30")


def test_each_configuration_sets_both_variables(tmp_path):
    crawler = FakeCrawler(Clock())
    make_benchmark(tmp_path, crawler, [3], [50]).run()

    assert starts(crawler) == [{"CRAWLER_WORKERS": "3", "CRAWL_THREADS": "50"}]


def test_configurations_run_one_at_a_time_and_the_crawler_is_stopped_between_them(tmp_path):
    crawler = FakeCrawler(Clock())
    make_benchmark(tmp_path, crawler).run()

    kinds = [event[0] for event in crawler.events if event[0] != "restore"]
    assert kinds.count("start") == 4
    assert not crawler.started_while_running
    start_positions = [i for i, kind in enumerate(kinds) if kind == "start"]
    assert all("stop" in kinds[a:b] for a, b in zip(start_positions, start_positions[1:]))
    assert kinds[-1] == "stop"


def test_the_rate_is_what_was_fetched_between_two_batches_a_duration_apart(tmp_path):
    crawler = FakeCrawler(Clock())
    results = make_benchmark(tmp_path, crawler).run()

    rates = {(r.configuration.processes, r.configuration.threads): r.fetches_per_minute for r in results}
    assert rates == {(1, 10): 60, (1, 25): 150, (2, 10): 120, (2, 25): 300}
    assert all(r.status == "OK" for r in results)


def test_the_window_is_not_cut_through_a_batch(tmp_path):
    # Each batch is a sixth of a minute's fetching, so a fixed window ending part way through
    # one would be wrong by up to that. Windows that open and close on batches are exact.
    crawler = FakeCrawler(Clock(), rate=lambda env: 60)
    [result] = make_benchmark(tmp_path, crawler, [1], [10], duration_seconds=25).run()

    assert result.fetches_per_minute == pytest.approx(60)


def test_a_crawler_that_never_finishes_a_batch_fails_the_configuration(tmp_path):
    crawler = FakeCrawler(Clock(), rate=lambda env: 0)
    [result] = make_benchmark(tmp_path, crawler, [1], [10]).run()

    assert result.status == "FAILED"
    assert "too slow" in result.error


def test_the_counter_from_before_a_restart_is_not_counted(tmp_path):
    crawler = FakeCrawler(Clock())
    crawler.banked = 1_000_000
    [result] = make_benchmark(tmp_path, crawler, [1], [10]).run()

    assert result.fetches_per_minute == 60


def test_every_combination_is_tried_even_when_more_threads_and_processes_stop_helping(tmp_path):
    flat = FakeCrawler(Clock(), rate=lambda env: 600)
    results = make_benchmark(tmp_path, flat, [1, 2, 3], [10, 25, 50]).run()

    assert [r.configuration for r in results] == all_configurations([1, 2, 3], [10, 25, 50])


def test_results_are_written_to_csv_as_they_complete_and_appended_to(tmp_path):
    path = tmp_path / "results.csv"
    make_benchmark(tmp_path, FakeCrawler(Clock()), [2], [25], csv_path=path).run()
    make_benchmark(tmp_path, FakeCrawler(Clock()), [1], [10], csv_path=path).run()

    rows = list(csv.DictReader(path.open()))
    assert [(r["processes"], r["threads"], r["fetches_per_minute"]) for r in rows] == [
        ("2", "25", "300.0"),
        ("1", "10", "60.0"),
    ]
    assert rows[0]["status"] == "OK"
    assert rows[0]["duration_seconds"] == "60"
    assert rows[0]["warmup_seconds"] == "20"
    assert rows[0]["timestamp"]
    assert path.read_text().count("timestamp,processes") == 1


def test_a_configuration_that_fails_is_recorded_and_the_rest_still_run(tmp_path):
    crawler = FakeCrawler(Clock(), fail_start_for="25")
    results = make_benchmark(tmp_path, crawler).run()

    assert [r.status for r in results] == ["OK", "FAILED", "OK", "FAILED"]
    assert "cannot start" in results[1].error
    assert results[1].fetches_per_minute is None
    assert not crawler.running
    assert crawler.events[-1] == ("restore", False)


@pytest.mark.parametrize("die_after_checks", [1, 2])
def test_a_crawler_that_dies_mid_wait_or_in_the_last_interval_is_a_failure(tmp_path, die_after_checks):
    crawler = FakeCrawler(Clock(), die_after_checks=die_after_checks)
    [result] = make_benchmark(tmp_path, crawler, [1], [10]).run()

    assert result.status == "FAILED"
    assert "the crawler died" in result.error


def test_a_crawler_that_dies_while_being_measured_is_a_failure(tmp_path):
    crawler = FakeCrawler(Clock(), die_after_checks=5)
    [result] = make_benchmark(tmp_path, crawler, [1], [10]).run()

    assert result.status == "FAILED"
    assert "the crawler died" in result.error


def test_an_interrupt_while_stopping_the_users_crawler_still_restores_it(tmp_path):
    crawler = FakeCrawler(Clock(), running_at_start=True, stop_existing_error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        make_benchmark(tmp_path, crawler).run()

    assert crawler.events[-1] == ("restore", True)


def test_an_unwritable_output_path_is_found_before_anything_is_stopped(tmp_path):
    crawler = FakeCrawler(Clock(), running_at_start=True)

    with pytest.raises(FileNotFoundError):
        make_benchmark(tmp_path, crawler, csv_path=tmp_path / "missing" / "results.csv").run()

    assert crawler.events == []


def test_a_crawler_that_was_running_is_stopped_first_and_restored_after(tmp_path):
    crawler = FakeCrawler(Clock(), running_at_start=True)
    make_benchmark(tmp_path, crawler, [1], [10]).run()

    assert crawler.events[0] == ("stop",)
    assert crawler.events[-1] == ("restore", True)
    assert not crawler.started_while_running


def test_ctrl_c_stops_the_crawler_restores_it_and_keeps_what_finished(tmp_path):
    crawler = FakeCrawler(Clock(), running_at_start=True)
    benchmark_run = make_benchmark(tmp_path, crawler)
    real_sleep = benchmark_run.sleep

    def interrupt_during_the_second_configuration(seconds):
        if len(starts(crawler)) == 2:
            raise KeyboardInterrupt
        real_sleep(seconds)

    benchmark_run.sleep = interrupt_during_the_second_configuration
    results = benchmark_run.run()

    assert [r.status for r in results] == ["OK", "INTERRUPTED"]
    assert len(starts(crawler)) == 2
    assert crawler.events[-2:] == [("stop",), ("restore", True)]
    assert not crawler.running
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["status"] for r in rows] == ["OK", "INTERRUPTED"]


def test_a_failure_to_stop_the_crawler_ends_the_benchmark_but_still_restores(tmp_path):
    class Stuck(FakeCrawler):
        def stop(self):
            raise RuntimeError("docker is not answering")

    crawler = Stuck(Clock())
    with pytest.raises(RuntimeError):
        make_benchmark(tmp_path, crawler).run()

    assert not starts(crawler)
    assert crawler.events[-1] == ("restore", False)


def test_configurations_too_big_for_memory_are_skipped_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "total_memory_mb", lambda: 2000)
    crawler = FakeCrawler(Clock())
    results = make_benchmark(tmp_path, crawler, [1, 5], [10], memory_per_process_mb=400).run()

    assert [r.status for r in results] == ["OK", "SKIPPED"]
    assert len(starts(crawler)) == 1


def test_the_report_ranks_by_rate_names_the_winner_and_breaks_ties_cheaply():
    results = [
        Result(Configuration(1, 10), "OK", 124),
        Result(Configuration(2, 50), "OK", 201),
        Result(Configuration(3, 50), "OK", 201),
        Result(Configuration(4, 10), "FAILED", error="cannot start"),
    ]

    report = format_report(results)

    lines = report.splitlines()
    rows = [line for line in lines if line[:1].isdigit() or line.startswith("-  ")]
    assert [row.split()[1:3] for row in rows] == [["2", "50"], ["3", "50"], ["1", "10"], ["4", "10"]]
    winner = report[report.index("WINNER") :]
    assert "CRAWLER_WORKERS=2" in winner and "CRAWL_THREADS=50" in winner and "Fetches per minute: 201" in winner


def test_the_report_has_no_winner_when_nothing_completed():
    report = format_report([Result(Configuration(1, 10), "FAILED", error="x")])

    assert "WINNER" not in report
    assert "no winner" in report


def test_compose_gets_the_settings_in_its_environment_and_restores_the_original_ones(monkeypatch):
    runs = []

    def run(command, env=None, check=None, capture_output=None, text=None):
        if command[0] == "docker" and command[1] == "inspect":
            environment = ["PATH=/bin", "CRAWLER_WORKERS=4", "CRAWL_THREADS=75", "MWMBL_API_KEY=secret"]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(environment))
        runs.append((command[4:], env.get("CRAWLER_WORKERS"), env.get("MWMBL_API_KEY")))
        return subprocess.CompletedProcess(command, 0, stdout="abc\n")

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    monkeypatch.delenv("CRAWLER_WORKERS", raising=False)
    compose = ComposeCrawler(Path("docker-compose.yml"))

    compose.stop_existing()
    compose.start({"CRAWLER_WORKERS": "3"})
    compose.stop()
    compose.restore(was_running=True)
    compose.restore(was_running=False)

    assert compose.original_env == {"CRAWLER_WORKERS": "4", "CRAWL_THREADS": "75"}
    assert runs == [
        (["ps", "-q", "mwmbl-crawler"], None, None),
        (["stop", "--timeout", "2", "mwmbl-crawler"], None, None),
        (["up", "-d", "--pull", "never", "mwmbl-crawler"], "3", None),
        (["stop", "--timeout", "2", "mwmbl-crawler"], None, None),
        (["up", "-d", "--pull", "never", "mwmbl-crawler"], "4", None),
    ]


@pytest.mark.parametrize(
    ("environment", "missing"),
    [
        ({"MWMBL_API_KEY": "", "MWMBL_CONTACT_INFO": None}, "MWMBL_API_KEY, MWMBL_CONTACT_INFO"),
        ({"MWMBL_API_KEY": "CREATE_ME", "MWMBL_CONTACT_INFO": "contact@example.com"}, "MWMBL_API_KEY"),
        ({"MWMBL_API_KEY": "secret", "MWMBL_CONTACT_INFO": "CHANGE_ME@example.com"}, "MWMBL_CONTACT_INFO"),
    ],
)
def test_compose_rejects_missing_settings_before_stopping_anything(monkeypatch, environment, missing):
    commands = []

    def run(command, **kwargs):
        commands.append(command[4:])
        config = {"services": {"mwmbl-crawler": {"environment": environment}}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(config))

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    with pytest.raises(SystemExit, match=missing) as error:
        ComposeCrawler(Path("docker-compose.yml")).validate()

    assert "secret" not in str(error.value)
    assert commands == [["config", "--format", "json", "mwmbl-crawler"]]


def test_compose_accepts_resolved_settings_and_custom_services(monkeypatch):
    environment = {"MWMBL_API_KEY": "secret", "MWMBL_CONTACT_INFO": "contact@example.com"}
    config = {"services": {"mwmbl-crawler": {"environment": environment}}}
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, json.dumps(config)),
    )
    compose = ComposeCrawler(Path("docker-compose.yml"))
    compose.validate()
    environment.update({"MWMBL_API_KEY": "", "CRAWL_SUBMIT_MODE": "off"})
    compose.validate()
    config["services"]["mwmbl-crawler"]["environment"] = {}
    compose.validate()


@pytest.mark.parametrize(("output", "expected"), [("12345\n", 12345), ("\n", 0)])
def test_compose_reads_the_fetch_counter_from_the_crawlers_redis(monkeypatch, output, expected):
    commands = []

    def run(command, **kwargs):
        commands.append(command[4:])
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(benchmark.subprocess, "run", run)

    assert ComposeCrawler(Path("docker-compose.yml")).fetches() == expected
    assert commands == [["exec", "-T", "redis_crawler", "redis-cli", "HGET", "crawl-domain-overlap", "fetches"]]


def test_a_process_crawler_reads_the_fetch_counter_from_redis(monkeypatch):
    class FakeRedis:
        def hget(self, key, field):
            return {("crawl-domain-overlap", "fetches"): b"42"}.get((key, field))

    urls = []
    monkeypatch.setenv("REDIS_URL", "redis://example:6379/3")
    monkeypatch.setattr(benchmark_process.Redis, "from_url", lambda url: urls.append(url) or FakeRedis())

    assert ProcessCrawler(["sleep", "60"]).fetches() == 42
    assert urls == ["redis://example:6379/3"]


def test_a_process_crawler_gets_its_environment_and_dies_with_its_group():
    crawler = ProcessCrawler(["sleep", "60"])

    crawler.start({"CRAWLER_WORKERS": "1"})
    crawler.ensure_running()
    process = crawler.process
    crawler.stop()

    assert process.poll() is not None
    crawler.stop()


def test_a_process_crawler_can_import_mwmbl_from_the_checkout_without_it_being_installed(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.chdir(tmp_path)
    # -S leaves out site-packages, where an installed mwmbl would be found.
    crawler = ProcessCrawler([sys.executable, "-S", "-c", "import mwmbl.crawler.benchmark"])

    crawler.start({})
    assert crawler.process.wait(timeout=30) == 0
    crawler.stop()


def test_the_process_runner_defaults_to_this_python_running_the_crawler_module(tmp_path):
    crawler = benchmark_process.process_crawler(None, tmp_path / "missing.env")

    assert crawler.command == [sys.executable, "-m", "mwmbl.crawl"]
    assert benchmark_process.process_crawler(["my-crawler"], tmp_path / "missing.env").command == ["my-crawler"]


def test_a_process_crawler_refuses_to_run_beside_another_crawler(monkeypatch):
    crawler = ProcessCrawler(["sleep", "60"])
    monkeypatch.setattr(ProcessCrawler, "_existing_crawlers", staticmethod(lambda: []))
    assert not crawler.is_running()

    with pytest.raises(SystemExit, match="already running"):
        crawler.stop_existing()


def test_workers_that_outlive_the_crawlers_leader_are_stopped_too(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark_process, "STOP_TIMEOUT_SECONDS", 5)
    pid_file = tmp_path / "worker.pid"
    crawler = ProcessCrawler(["sh", "-c", f"sleep 60 & echo $! > {pid_file}; exit 0"])
    crawler.start({})
    crawler.process.wait()
    worker = psutil.Process(int(pid_file.read_text()))
    assert worker.is_running()

    crawler.stop()

    assert not worker.is_running() or worker.status() == psutil.STATUS_ZOMBIE


def test_the_machines_memory_can_be_read_on_this_platform():
    assert benchmark.total_memory_mb() > 0


def parse(*arguments):
    parser = argparse.ArgumentParser()
    benchmark.add_arguments(parser)
    return parser.parse_args(arguments)


def test_the_defaults_are_the_documented_grid_and_timings():
    args = parse()

    assert (args.processes, args.threads) == ([1, 2, 3, 4, 5], [10, 25, 50, 100])
    assert (args.warmup, args.duration, args.runner) == (300, 1800, "compose")
    assert args.output.name.startswith("benchmark-results-fetches-") and args.output.suffix == ".csv"


def test_every_search_space_and_timing_default_can_be_overridden():
    args = parse("--processes", "1,3", "--threads", "8", "--warmup", "90s", "--duration", "2h")

    assert (args.processes, args.threads, args.warmup, args.duration) == ([1, 3], [8], 90, 7200)


@pytest.fixture
def sigterm_handler_restored():
    original = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, original)


@pytest.mark.parametrize("runner", ["compose", "process"])
def test_main_runs_the_grid_with_the_chosen_runner_and_prints_the_winner(
    tmp_path, monkeypatch, capsys, sigterm_handler_restored, runner
):
    clock = Clock()
    crawler = FakeCrawler(clock)
    commands = []
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.touch()
    monkeypatch.setattr(benchmark, "ComposeCrawler", lambda compose_file, env_file: crawler)
    monkeypatch.setattr(benchmark_process, "ProcessCrawler", lambda command: commands.append(command) or crawler)
    monkeypatch.setattr(benchmark, "Benchmark", partial(Benchmark, sleep=clock.sleep, clock=clock))
    output = tmp_path / "r.csv"

    exit_code = benchmark.main(
        ["--processes", "1", "--threads", "10,25", "--warmup", "10s", "--duration", "40s", "--output", str(output)]
        + ["--runner", runner, "--crawler-command", "my-crawler --flag", "--no-memory-check"]
        + ["--env-file", str(tmp_path / "missing.env"), "--compose-file", str(compose_file)]
    )

    report = capsys.readouterr().out
    assert exit_code == 0
    assert "WINNER" in report and "CRAWL_THREADS=25" in report
    assert [row["threads"] for row in csv.DictReader(output.open())] == ["10", "25"]
    assert commands == ([["my-crawler", "--flag"]] if runner == "process" else [])


def test_the_env_file_beside_the_compose_file_is_loaded_by_default(tmp_path, monkeypatch, sigterm_handler_restored):
    (tmp_path / ".env").write_text("BENCHMARK_TEST_SETTING=from-the-env-file\nBENCHMARK_TEST_SET_ALREADY=ignored\n")
    monkeypatch.setenv("BENCHMARK_TEST_SETTING", "")
    monkeypatch.delenv("BENCHMARK_TEST_SETTING")
    monkeypatch.setenv("BENCHMARK_TEST_SET_ALREADY", "from-the-shell")
    clock = Clock()
    seen = {}

    class Recording(FakeCrawler):
        def start(self, env):
            seen["setting"] = benchmark.os.environ.get("BENCHMARK_TEST_SETTING")
            super().start(env)

    crawler = Recording(clock)
    monkeypatch.setattr(benchmark_process, "ProcessCrawler", lambda command: crawler)
    monkeypatch.setattr(benchmark, "Benchmark", partial(Benchmark, sleep=clock.sleep, clock=clock))
    arguments = ["--runner", "process", "--processes", "1", "--threads", "10", "--no-memory-check"]

    benchmark.main(
        arguments + ["--compose-file", str(tmp_path / "docker-compose.yml")] + ["--output", str(tmp_path / "r.csv")]
    )

    assert seen["setting"] == "from-the-env-file"
    assert benchmark.os.environ["BENCHMARK_TEST_SET_ALREADY"] == "from-the-shell"


def test_main_returns_failure_when_no_configuration_completes(tmp_path, monkeypatch, sigterm_handler_restored):
    crawler = FakeCrawler(Clock(), fail_start_for="10")
    monkeypatch.setattr(benchmark_process, "ProcessCrawler", lambda command: crawler)
    monkeypatch.setattr(benchmark, "Benchmark", partial(Benchmark, sleep=crawler.clock.sleep, clock=crawler.clock))

    assert (
        benchmark.main(
            ["--runner", "process", "--processes", "1", "--threads", "10", "--output", str(tmp_path / "r.csv")]
        )
        == 1
    )


def test_sigterm_is_handled_like_ctrl_c():
    with pytest.raises(KeyboardInterrupt):
        benchmark.interrupt(signal.SIGTERM, None)


def test_the_compose_runner_refuses_a_directory_with_no_compose_file(tmp_path):
    with pytest.raises(SystemExit, match="directory you start your crawler from"):
        benchmark.main(["--compose-file", str(tmp_path / "docker-compose.yml"), "--output", str(tmp_path / "r.csv")])


def test_compose_is_given_the_env_file_when_there_is_one(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    ComposeCrawler(Path("docker-compose.yml"), Path("crawler.env")).stop()

    assert commands[0][:6] == ["docker", "compose", "-f", "docker-compose.yml", "--env-file", "crawler.env"]


def test_the_benchmark_runs_as_a_lone_file_with_only_the_standard_library(tmp_path):
    # -S leaves out site-packages, so any third-party import fails. Volunteers get this file alone.
    script = Path(benchmark.__file__)
    completed = subprocess.run(
        [sys.executable, "-S", str(script), "--compose-file", str(tmp_path / "docker-compose.yml")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "directory you start your crawler from" in completed.stderr


def test_compose_knows_whether_the_container_is_up(monkeypatch):
    output = {"stdout": "abc123\n"}
    monkeypatch.setattr(
        benchmark.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, **output)
    )
    compose = ComposeCrawler(Path("docker-compose.yml"))

    assert compose.is_running()
    compose.ensure_running()
    output["stdout"] = ""
    assert not compose.is_running()
    with pytest.raises(RuntimeError, match="logs --tail 100 mwmbl-crawler"):
        compose.ensure_running()


def test_the_mwmbl_command_runs_the_benchmark_subcommand(monkeypatch):
    received = []
    monkeypatch.setattr(benchmark, "run", lambda args: received.append(args) or 0)

    assert cli.main(["benchmark", "--processes", "2", "--threads", "5"]) == 0

    assert (received[0].processes, received[0].threads) == ([2], [5])
    with pytest.raises(SystemExit):
        cli.main([])


def run_the_shell_script(tmp_path, docker_script):
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    docker = stub_bin / "docker"
    docker.write_text(f"#!/bin/sh\n{docker_script}\n")
    docker.chmod(0o755)
    script = Path(benchmark.__file__).parents[2] / "deploy" / "crawler" / "benchmark.sh"
    environment = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "MWMBL_BENCHMARK_URL": "file:///nonexistent",
    }
    return subprocess.run(["sh", str(script)], cwd=tmp_path, env=environment, capture_output=True, text=True)


def test_the_shell_script_in_a_checkout_runs_the_benchmark_beside_it_and_does_not_download(tmp_path):
    completed = run_the_shell_script(tmp_path, "exit 0")

    assert "directory you start your crawler from" in completed.stderr
    assert "could not download" not in completed.stderr


def test_the_shell_script_tells_a_user_outside_the_docker_group_how_to_join_it(tmp_path):
    docker_script = (
        '[ "$1" = info ] && { echo "permission denied while trying to connect to the docker API" >&2; exit 1; }; exit 0'
    )

    completed = run_the_shell_script(tmp_path, docker_script)

    assert completed.returncode == 1
    assert "usermod -aG docker" in completed.stderr
    assert "directory you start your crawler from" not in completed.stderr


def test_the_shell_script_says_when_the_docker_daemon_is_not_running(tmp_path):
    docker_script = '[ "$1" = info ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }; exit 0'

    completed = run_the_shell_script(tmp_path, docker_script)

    assert completed.returncode == 1
    assert "Is it running?" in completed.stderr
    assert "Cannot connect to the Docker daemon" in completed.stderr
