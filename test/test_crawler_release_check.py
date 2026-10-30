"""Tests for the CI check that crawler changes raise CRAWLER_VERSION."""

import pytest

from mwmbl.crawler import release_check
from mwmbl.crawler.release_check import changed_crawler_files, crawler_source_files, main, parse_version
from mwmbl.crawler.version import CRAWLER_VERSION

CRAWLER_FILE = "mwmbl/crawler/retrieve.py"


def test_crawler_source_files_follow_the_entry_point():
    crawler_files = crawler_source_files()

    assert {"mwmbl/crawl.py", "mwmbl/crawler/retrieve.py", "mwmbl/settings_crawler.py"} <= crawler_files
    assert "mwmbl/platform/api.py" not in crawler_files


def test_changed_crawler_files_ignore_server_only_changes_and_the_version_itself():
    changed = [
        "mwmbl/crawler/retrieve.py",
        "mwmbl/platform/api.py",
        "mwmbl/crawler/version.py",
        "uv.lock",
        "mwmbl_rank/src/lib.rs",
        "mwmbl/resources/document_counts.json",
        "mwmbl/templates/index.html",
        "README.md",
    ]

    crawler_changes = changed_crawler_files(changed, {"mwmbl/crawler/retrieve.py", "mwmbl/crawler/version.py"})

    assert crawler_changes == [
        "mwmbl/crawler/retrieve.py",
        "uv.lock",
        "mwmbl_rank/src/lib.rs",
        "mwmbl/resources/document_counts.json",
    ]


def test_parse_version_compares_numerically():
    assert parse_version('CRAWLER_VERSION: str = "1.10.0"\n') > parse_version('CRAWLER_VERSION: str = "1.9.3"\n')


def test_parse_version_rejects_a_version_it_cannot_compare():
    with pytest.raises(AssertionError):
        parse_version('CRAWLER_VERSION: str = "1.0"\n')


def _fake_git(monkeypatch, changed_files: list[str], base_version: str):
    def git(*args: str) -> str:
        if args[0] == "diff":
            return "\n".join(changed_files) + "\n"
        assert args == ("show", "base:mwmbl/crawler/version.py")
        return f'CRAWLER_VERSION: str = "{base_version}"\n'

    monkeypatch.setattr(release_check, "_git", git)
    monkeypatch.setattr(release_check, "crawler_source_files", lambda: {CRAWLER_FILE})


def test_main_passes_when_no_crawler_code_changed(monkeypatch):
    _fake_git(monkeypatch, ["mwmbl/platform/api.py"], CRAWLER_VERSION)

    assert main("base") == 0


def test_main_passes_when_the_version_was_raised(monkeypatch):
    _fake_git(monkeypatch, [CRAWLER_FILE], "0.0.1")

    assert main("base") == 0


def test_main_fails_and_lists_crawler_changes_without_a_version_bump(monkeypatch, capsys):
    _fake_git(monkeypatch, [CRAWLER_FILE, "mwmbl/platform/api.py"], CRAWLER_VERSION)

    assert main("base") == 1
    output = capsys.readouterr().out
    assert CRAWLER_FILE in output
    assert "mwmbl/platform/api.py" not in output


def test_git_runs_in_the_repository():
    assert release_check._git("rev-parse", "--is-inside-work-tree") == "true\n"
