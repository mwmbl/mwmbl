"""
The crawler supervisor: it respawns dead workers, gives up on a crash-looping indexer,
and the module-level helpers and CLI entry point delegate to it.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from mwmbl import crawl
from mwmbl.crawl import Crawler


class LoopStopped(BaseException):
    """Breaks the supervisor's while True once the test has seen what it came for."""


def _process(alive: bool, exitcode: int | None = None) -> MagicMock:
    process = MagicMock()
    process.is_alive.return_value = alive
    process.exitcode = exitcode
    process.pid = 4242
    return process


@pytest.fixture
def supervisor_environment():
    with (
        patch.object(crawl, "validate_environment"),
        patch.object(Crawler, "check_redis"),
        patch.object(Crawler, "refresh_blacklist_snapshot"),
        patch.object(crawl, "CRAWLER_WORKERS", 2),
    ):
        yield


def test_run_gives_up_when_the_indexer_keeps_crashing(supervisor_environment):
    with (
        patch.object(crawl, "Process", side_effect=lambda target: _process(alive=False, exitcode=1)),
        patch.object(crawl.time, "sleep"),
    ):
        with pytest.raises(RuntimeError, match="crashed 6 times in the last hour"):
            Crawler().run()


def test_run_respawns_a_dead_batch_worker(supervisor_environment):
    started = []

    def make_process(target):
        process = _process(alive=target.__name__ != "process_batch_continuously" or not started)
        started.append(process)
        return process

    sleeps = []

    def stop_after_first_pass(seconds):
        sleeps.append(seconds)
        if seconds == 10:
            raise LoopStopped

    with (
        patch.object(crawl, "Process", side_effect=make_process),
        patch.object(crawl.time, "sleep", side_effect=stop_after_first_pass),
    ):
        with pytest.raises(LoopStopped):
            Crawler().run()

    # Two workers and an indexer at startup, then the dead first worker is replaced
    # (the indexer is alive, so it is not).
    assert len(started) == 4
    started[3].start.assert_called_once()


def test_run_rejects_zero_workers():
    with (
        patch.object(crawl, "validate_environment"),
        patch.object(Crawler, "check_redis"),
        patch.object(crawl, "CRAWLER_WORKERS", 0),
    ):
        with pytest.raises(AssertionError, match="CRAWLER_WORKERS"):
            Crawler().run()


def test_run_keeps_crawling_when_a_snapshot_refresh_fails(supervisor_environment):
    def stop_after_first_pass(seconds):
        if seconds == 10:
            raise LoopStopped

    with (
        override_settings(BLACKLIST_SNAPSHOT_REFRESH_SECONDS=0),
        patch.object(Crawler, "refresh_blacklist_snapshot", side_effect=[None, RuntimeError("no network")]) as refresh,
        patch.object(crawl, "Process", side_effect=lambda target: _process(alive=True)),
        patch.object(crawl.time, "sleep", side_effect=stop_after_first_pass),
    ):
        with pytest.raises(LoopStopped):
            Crawler().run()

    assert refresh.call_count == 2


def test_a_failed_snapshot_publisher_is_an_error():
    with patch.object(crawl, "Process", return_value=_process(alive=False, exitcode=1)):
        with pytest.raises(RuntimeError, match="exit code 1"):
            Crawler().refresh_blacklist_snapshot()


def test_the_queue_and_each_batch_share_one_snapshot():
    crawler = Crawler()
    with patch.object(crawl, "SnapshotBlacklist") as snapshot_blacklist:
        first = crawler.new_blacklist_provider()
        second = crawler.new_blacklist_provider()

    snapshot_blacklist.assert_called_once()
    assert first is not second
    assert first.providers == second.providers == [snapshot_blacklist.return_value]


def test_run_once_checks_the_environment_then_crawls_and_indexes():
    crawler = Crawler()
    with (
        patch.object(crawl, "validate_environment") as validate,
        patch.object(Crawler, "check_redis") as check_redis,
        patch.object(Crawler, "refresh_blacklist_snapshot") as refresh_blacklist_snapshot,
        patch.object(Crawler, "process_batch") as process_batch,
        patch.object(Crawler, "run_indexing") as run_indexing,
    ):
        crawler.run_once()

    validate.assert_called_once()
    check_redis.assert_called_once()
    refresh_blacklist_snapshot.assert_called_once()
    process_batch.assert_called_once()
    run_indexing.assert_called_once()


@pytest.mark.parametrize("name", ["check_redis", "process_batch", "run_indexing", "run", "run_once"])
def test_module_level_helpers_delegate_to_the_default_crawler(monkeypatch, name):
    default_crawler = MagicMock()
    monkeypatch.setattr(crawl, "_default_crawler", default_crawler)

    result = getattr(crawl, name)()

    getattr(default_crawler, name).assert_called_once_with()
    assert result is getattr(default_crawler, name).return_value


def test_default_crawler_is_created_once(monkeypatch):
    monkeypatch.setattr(crawl, "_default_crawler", None)

    first = crawl.get_default_crawler()
    second = crawl.get_default_crawler()

    assert first is second


@pytest.mark.parametrize("argv, expected", [(["mwmbl-crawl"], "run"), (["mwmbl-crawl", "--once"], "run_once")])
def test_main_chooses_between_continuous_and_single_pass(monkeypatch, argv, expected):
    monkeypatch.setattr(sys, "argv", argv)
    with patch.object(crawl, "run") as run, patch.object(crawl, "run_once") as run_once:
        crawl.main()

    called, not_called = (run, run_once) if expected == "run" else (run_once, run)
    called.assert_called_once()
    not_called.assert_not_called()


@pytest.fixture
def fresh_curated_domains_cache(monkeypatch):
    monkeypatch.setattr(crawl, "_curated_domains_cache", {"stale.example"})
    monkeypatch.setattr(crawl, "_curated_domains_fetched_at", 0.0)


def test_curated_domains_are_fetched_and_then_served_from_the_cache(fresh_curated_domains_cache, monkeypatch):
    response = MagicMock()
    response.json.return_value = {"domains": [{"name": "a.example"}, {"name": "b.example"}]}
    get = MagicMock(return_value=response)
    monkeypatch.setattr(crawl.requests, "get", get)
    monkeypatch.setattr(crawl.time, "monotonic", lambda: 1000.0)

    first = crawl._fetch_curated_domains()
    second = crawl._fetch_curated_domains()

    assert first == second == {"a.example", "b.example"}
    get.assert_called_once()


def test_a_failed_fetch_keeps_serving_the_cached_domains(fresh_curated_domains_cache, monkeypatch):
    monkeypatch.setattr(crawl.requests, "get", MagicMock(side_effect=crawl.requests.ConnectionError("down")))
    monkeypatch.setattr(crawl.time, "monotonic", lambda: 1000.0)

    assert crawl._fetch_curated_domains() == {"stale.example"}
