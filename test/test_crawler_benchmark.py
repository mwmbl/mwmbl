"""The crawler benchmark: which configurations it runs, how it runs them and what it leaves behind."""

import argparse
import csv
import json
import signal
import subprocess
from functools import partial
from pathlib import Path

import psutil
import pytest
import requests

from mwmbl import cli
from mwmbl.crawler import benchmark
from mwmbl.crawler.benchmark import (
    Benchmark,
    ComposeCrawler,
    Configuration,
    DeviceReading,
    DeviceScores,
    ProcessCrawler,
    Result,
    all_configurations,
    format_report,
    parse_counts,
    parse_duration,
)

DEVICE = "crawler-test"


class FakeCrawler:
    """Records what it was asked to do, and fails the way a real crawler could be made to."""

    def __init__(self, running_at_start=False, fail_start_for=None, die_after_checks=None, stop_existing_error=None):
        self.running = running_at_start
        self.fail_start_for = fail_start_for
        self.die_after_checks = die_after_checks
        self.stop_existing_error = stop_existing_error
        self.checks = 0
        self.events: list[tuple] = []
        self.started_while_running = False

    def is_running(self):
        return self.running

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
        self.events.append(("start", env))

    def ensure_running(self):
        self.checks += 1
        if self.die_after_checks is not None and self.checks > self.die_after_checks:
            raise RuntimeError("the crawler died")

    def stop(self):
        self.running = False
        self.events.append(("stop",))

    def restore(self, was_running):
        self.events.append(("restore", was_running))


class FakeScores:
    """The server's view: a window opened by a read without `since` gains threads x processes of the
    configuration the crawler was last started with, and is reported by the read that gives `since`."""

    def __init__(self, crawler, device_exists=True, results=None):
        self.crawler = crawler
        self.device_exists = device_exists
        self.results = results
        self.since_given = []

    def check_access(self):
        pass

    def read(self, since=None):
        if since is None:
            return DeviceReading(False, 0, 1700000000.0, "unknown")
        self.since_given.append(since)
        env = [event for event in self.crawler.events if event[0] == "start"][-1][1]
        gain = int(env["CRAWL_THREADS"]) * int(env["CRAWLER_WORKERS"]) if self.results is None else self.results
        return DeviceReading(self.device_exists, gain, since + 600, "1.2.3")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_benchmark(tmp_path, crawler, scores=None, configurations=None, **overrides):
    clock = Clock()
    arguments = {
        "configurations": configurations or all_configurations([1, 2], [10, 25]),
        "crawler": crawler,
        "scores": scores or FakeScores(crawler),
        "device_name": DEVICE,
        "warmup_seconds": 120,
        "duration_seconds": 600,
        "csv_path": tmp_path / "results.csv",
        "memory_per_process_mb": None,
        "sleep": clock.sleep,
        "clock": clock,
        **overrides,
    }
    return Benchmark(**arguments)


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


def test_a_zero_duration_is_shown_in_seconds():
    assert benchmark.format_duration(0) == "0s"
    assert benchmark.format_duration(1800) == "30m"


def test_durations_have_a_unit():
    assert parse_duration("90s") == 90
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration("30")


def test_each_configuration_sets_both_variables_and_the_device_name(tmp_path):
    crawler = FakeCrawler()
    make_benchmark(tmp_path, crawler, configurations=[Configuration(3, 50)]).run()

    [start] = [event for event in crawler.events if event[0] == "start"]
    assert start[1] == {"CRAWLER_WORKERS": "3", "CRAWL_THREADS": "50", "MWMBL_DEVICE_NAME": DEVICE}


def test_configurations_run_one_at_a_time_and_the_crawler_is_stopped_between_them(tmp_path):
    crawler = FakeCrawler()
    make_benchmark(tmp_path, crawler).run()

    kinds = [event[0] for event in crawler.events if event[0] != "restore"]
    assert kinds.count("start") == 4
    assert not crawler.started_while_running
    starts = [i for i, kind in enumerate(kinds) if kind == "start"]
    assert all("stop" in kinds[a:b] for a, b in zip(starts, starts[1:]))
    assert kinds[-1] == "stop"


def test_the_score_is_what_the_device_gained_after_warmup(tmp_path):
    crawler = FakeCrawler()
    results = make_benchmark(tmp_path, crawler).run()

    scores = {(r.configuration.processes, r.configuration.threads): r.index_score for r in results}
    assert scores == {(1, 10): 10, (1, 25): 25, (2, 10): 20, (2, 25): 50}
    assert all(r.status == "OK" and r.crawler_version == "1.2.3" for r in results)


def test_the_window_is_bounded_by_the_servers_clock_from_the_end_of_warmup(tmp_path):
    crawler = FakeCrawler()
    scores = FakeScores(crawler)
    make_benchmark(tmp_path, crawler, scores, [Configuration(1, 10), Configuration(2, 10)]).run()

    assert scores.since_given == [1700000000.0, 1700000000.0]


def test_results_are_written_to_csv_as_they_complete_and_appended_to(tmp_path):
    crawler = FakeCrawler()
    path = tmp_path / "results.csv"
    make_benchmark(tmp_path, crawler, configurations=[Configuration(2, 25)], csv_path=path).run()
    make_benchmark(tmp_path, crawler, configurations=[Configuration(1, 10)], csv_path=path).run()

    rows = list(csv.DictReader(path.open()))
    assert [(r["processes"], r["threads"], r["index_score"]) for r in rows] == [("2", "25", "50"), ("1", "10", "10")]
    row = rows[0]
    assert row["status"] == "OK"
    assert row["hostname"] == DEVICE
    assert "device_name" not in row
    assert row["crawler_version"] == "1.2.3"
    assert row["duration_seconds"] == "600"
    assert row["warmup_seconds"] == "120"
    assert row["timestamp"]
    assert path.read_text().count("timestamp,hostname") == 1


def test_a_configuration_that_fails_is_recorded_and_the_rest_still_run(tmp_path):
    crawler = FakeCrawler(fail_start_for="25")
    results = make_benchmark(tmp_path, crawler).run()

    assert [r.status for r in results] == ["OK", "FAILED", "OK", "FAILED"]
    assert "cannot start" in results[1].error
    assert results[1].index_score is None
    assert not crawler.running
    assert crawler.events[-1] == ("restore", False)


@pytest.mark.parametrize("scores_kwargs", [{"device_exists": False}, {"results": 0}])
def test_nothing_submitted_is_a_setup_error_that_stops_the_run_but_still_restores(tmp_path, scores_kwargs):
    crawler = FakeCrawler(running_at_start=True)
    scores = FakeScores(crawler, **scores_kwargs)
    results = make_benchmark(tmp_path, crawler, scores).run()

    assert [r.status for r in results] == ["FAILED"]
    assert "MWMBL_REMOTE_SERVER" in results[0].error
    assert len([e for e in crawler.events if e[0] == "start"]) == 1
    assert crawler.events[-1] == ("restore", True)


@pytest.mark.parametrize("die_after_checks", [1, 2])
def test_a_crawler_that_dies_mid_wait_or_in_the_last_interval_is_a_failure(tmp_path, die_after_checks):
    crawler = FakeCrawler(die_after_checks=die_after_checks)
    [result] = make_benchmark(tmp_path, crawler, configurations=[Configuration(1, 10)]).run()

    assert result.status == "FAILED"
    assert "the crawler died" in result.error


def test_an_interrupt_while_stopping_the_users_crawler_still_restores_it(tmp_path):
    crawler = FakeCrawler(running_at_start=True, stop_existing_error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        make_benchmark(tmp_path, crawler).run()

    assert crawler.events[-1] == ("restore", True)


def test_an_unwritable_output_path_is_found_before_anything_is_stopped(tmp_path):
    crawler = FakeCrawler(running_at_start=True)

    with pytest.raises(FileNotFoundError):
        make_benchmark(tmp_path, crawler, csv_path=tmp_path / "missing" / "results.csv").run()

    assert crawler.events == []


def test_a_crawler_that_was_running_is_stopped_first_and_restored_after(tmp_path):
    crawler = FakeCrawler(running_at_start=True)
    make_benchmark(tmp_path, crawler, configurations=[Configuration(1, 10)]).run()

    assert crawler.events[0] == ("stop",)
    assert crawler.events[-1] == ("restore", True)
    assert not crawler.started_while_running


def test_ctrl_c_stops_the_crawler_restores_it_and_keeps_what_finished(tmp_path):
    crawler = FakeCrawler(running_at_start=True)
    benchmark_run = make_benchmark(tmp_path, crawler)
    real_sleep = benchmark_run.sleep
    sleeps = []

    def interrupt_during_the_second_configuration(seconds):
        sleeps.append(seconds)
        if len([e for e in crawler.events if e[0] == "start"]) == 2:
            raise KeyboardInterrupt
        real_sleep(seconds)

    benchmark_run.sleep = interrupt_during_the_second_configuration
    results = benchmark_run.run()

    assert [r.status for r in results] == ["OK", "INTERRUPTED"]
    assert len([e for e in crawler.events if e[0] == "start"]) == 2
    assert crawler.events[-2:] == [("stop",), ("restore", True)]
    assert not crawler.running
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert [r["status"] for r in rows] == ["OK", "INTERRUPTED"]


def test_a_failure_to_stop_the_crawler_ends_the_benchmark_but_still_restores(tmp_path):
    class Stuck(FakeCrawler):
        def stop(self):
            raise RuntimeError("docker is not answering")

    crawler = Stuck()
    with pytest.raises(RuntimeError):
        make_benchmark(tmp_path, crawler).run()

    assert not [e for e in crawler.events if e[0] == "start"]
    assert crawler.events[-1] == ("restore", False)


def test_configurations_too_big_for_memory_are_skipped_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "total_memory_mb", lambda: 2000)
    crawler = FakeCrawler()
    results = make_benchmark(
        tmp_path, crawler, configurations=all_configurations([1, 5], [10]), memory_per_process_mb=400
    ).run()

    assert [r.status for r in results] == ["OK", "SKIPPED"]
    assert len([e for e in crawler.events if e[0] == "start"]) == 1


def test_the_report_ranks_by_score_names_the_winner_and_breaks_ties_cheaply():
    results = [
        Result(Configuration(1, 10), "OK", 124),
        Result(Configuration(2, 50), "OK", 201),
        Result(Configuration(3, 50), "OK", 201),
        Result(Configuration(4, 10), "FAILED", error="cannot start"),
    ]

    report = format_report(results, DEVICE, 1800)

    lines = report.splitlines()
    rows = [line for line in lines if line[:1].isdigit() or line.startswith("-  ")]
    assert [row.split()[1:3] for row in rows] == [["2", "50"], ["3", "50"], ["1", "10"], ["4", "10"]]
    assert f"Device: {DEVICE}" in report
    assert "30m" in report
    winner = report[report.index("WINNER") :]
    assert "CRAWLER_WORKERS=2" in winner and "CRAWL_THREADS=50" in winner and "Index score: 201" in winner


def test_the_report_has_no_winner_when_nothing_completed():
    report = format_report([Result(Configuration(1, 10), "FAILED", error="x")], DEVICE, 60)

    assert "WINNER" not in report
    assert "no winner" in report


def test_scores_are_read_from_the_server_with_the_crawl_key(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"known": True, "results_since": 9, "results_total": 99, "crawler_version": None, "server_time": 5.5}

    def get(url, params, headers, timeout):
        calls.append((url, params, headers["X-API-Key"]))
        return Response()

    monkeypatch.setattr(benchmark.requests, "get", get)
    scores = DeviceScores("https://api.example", "key-1", DEVICE)

    assert scores.read(since=4.0) == DeviceReading(True, 9, 5.5, "unknown")
    scores.check_access()
    url = "https://api.example/api/v1/crawler/device-results"
    assert calls == [(url, {"device_name": DEVICE, "since": 4.0}, "key-1"), (url, {"device_name": DEVICE}, "key-1")]


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
        (["stop", "mwmbl-crawler"], None, None),
        (["up", "-d", "--pull", "never", "mwmbl-crawler"], "3", None),
        (["stop", "mwmbl-crawler"], None, None),
        (["up", "-d", "--pull", "never", "mwmbl-crawler"], "4", None),
    ]


def test_a_process_crawler_gets_its_environment_and_dies_with_its_group():
    crawler = ProcessCrawler(["sleep", "60"])

    crawler.start({"CRAWLER_WORKERS": "1"})
    crawler.ensure_running()
    process = crawler.process
    crawler.stop()

    assert process.poll() is not None
    crawler.stop()


def test_a_process_crawler_refuses_to_run_beside_another_crawler(monkeypatch):
    crawler = ProcessCrawler(["sleep", "60"])
    monkeypatch.setattr(ProcessCrawler, "_existing_crawlers", staticmethod(lambda: []))
    assert not crawler.is_running()

    with pytest.raises(SystemExit, match="already running"):
        crawler.stop_existing()


def test_workers_that_outlive_the_crawlers_leader_are_stopped_too(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "STOP_TIMEOUT_SECONDS", 5)
    pid_file = tmp_path / "worker.pid"
    crawler = ProcessCrawler(["sh", "-c", f"sleep 60 & echo $! > {pid_file}; exit 0"])
    crawler.start({})
    crawler.process.wait()
    worker = psutil.Process(int(pid_file.read_text()))
    assert worker.is_running()

    crawler.stop()

    assert not worker.is_running() or worker.status() == psutil.STATUS_ZOMBIE


def test_a_read_that_fails_transiently_is_retried(monkeypatch):
    attempts = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"known": True, "results_since": 1, "server_time": 2.0}

    def flaky(url, params, headers, timeout):
        attempts.append(1)
        if len(attempts) < benchmark.READ_ATTEMPTS:
            raise requests.ConnectionError("reset")
        return Response()

    monkeypatch.setattr(benchmark.requests, "get", flaky)
    monkeypatch.setattr(benchmark.time, "sleep", lambda seconds: None)

    assert DeviceScores("https://api.example", "key", DEVICE).read().results_since == 1
    assert len(attempts) == benchmark.READ_ATTEMPTS


def test_a_read_gives_up_after_the_last_attempt(monkeypatch):
    attempts = []

    def down(url, params, headers, timeout):
        attempts.append(1)
        raise requests.ConnectionError("down")

    monkeypatch.setattr(benchmark.requests, "get", down)
    monkeypatch.setattr(benchmark.time, "sleep", lambda seconds: None)

    with pytest.raises(requests.ConnectionError):
        DeviceScores("https://api.example", "key", DEVICE).read()
    assert len(attempts) == benchmark.READ_ATTEMPTS


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
    assert args.output.name.startswith("benchmark-results-") and args.output.suffix == ".csv"


def test_every_search_space_and_timing_default_can_be_overridden():
    args = parse("--processes", "1,3", "--threads", "8", "--warmup", "90s", "--duration", "2h", "--output", "r.csv")

    assert (args.processes, args.threads, args.warmup, args.duration) == ([1, 3], [8], 90, 7200)
    assert args.output == Path("r.csv")


@pytest.fixture
def sigterm_handler_restored():
    original = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, original)


@pytest.mark.parametrize("runner", ["compose", "process"])
def test_main_runs_the_grid_with_the_chosen_runner_and_prints_the_winner(
    tmp_path, monkeypatch, capsys, sigterm_handler_restored, runner
):
    crawler = FakeCrawler()
    commands = []
    clock = Clock()
    monkeypatch.setattr(benchmark, "MWMBL_API_KEY", "key")
    monkeypatch.setattr(benchmark, "ComposeCrawler", lambda compose_file: crawler)
    monkeypatch.setattr(benchmark, "ProcessCrawler", lambda command: commands.append(command) or crawler)
    monkeypatch.setattr(benchmark, "DeviceScores", lambda server, api_key, device_name: FakeScores(crawler))
    monkeypatch.setattr(benchmark, "Benchmark", partial(Benchmark, sleep=clock.sleep, clock=clock))
    output = tmp_path / "r.csv"

    exit_code = benchmark.main(
        ["--processes", "1", "--threads", "10,25", "--warmup", "1m", "--duration", "2m", "--output", str(output)]
        + ["--device-name", DEVICE, "--runner", runner, "--crawler-command", "my-crawler --flag", "--no-memory-check"]
    )

    report = capsys.readouterr().out
    assert exit_code == 0
    assert "WINNER" in report and "CRAWL_THREADS=25" in report
    assert [row["threads"] for row in csv.DictReader(output.open())] == ["10", "25"]
    assert commands == ([["my-crawler", "--flag"]] if runner == "process" else [])


def test_main_needs_the_crawl_key_to_read_scores(monkeypatch):
    monkeypatch.setattr(benchmark, "MWMBL_API_KEY", " ")

    with pytest.raises(SystemExit, match="MWMBL_API_KEY"):
        benchmark.main([])


def test_the_device_name_is_the_flag_then_the_environment_then_a_stored_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(benchmark, "MWMBL_DEVICE_NAME", "")

    generated = benchmark.device_name_for(parse())
    assert generated.startswith("crawler-")
    assert benchmark.device_name_for(parse()) == generated
    monkeypatch.setattr(benchmark, "MWMBL_DEVICE_NAME", "from-environment")
    assert benchmark.device_name_for(parse()) == "from-environment"
    assert benchmark.device_name_for(parse("--device-name", "from-flag")) == "from-flag"


def test_sigterm_is_handled_like_ctrl_c():
    with pytest.raises(KeyboardInterrupt):
        benchmark.interrupt(signal.SIGTERM, None)


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
    with pytest.raises(RuntimeError, match="stopped"):
        compose.ensure_running()


def test_the_mwmbl_command_runs_the_benchmark_subcommand(monkeypatch):
    received = []
    monkeypatch.setattr(benchmark, "run", lambda args: received.append(args) or 0)

    assert cli.main(["benchmark", "--processes", "2", "--threads", "5"]) == 0

    assert (received[0].processes, received[0].threads) == ([2], [5])
    with pytest.raises(SystemExit):
        cli.main([])
