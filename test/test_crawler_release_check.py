"""Tests for the CI check that crawler changes raise CRAWLER_VERSION."""

import subprocess

import pytest

from mwmbl.crawler import release_check
from mwmbl.crawler.release_check import (
    NO_CRAWLER_CHANGES,
    changed_crawler_files,
    check_commit_message,
    crawler_source_files,
    main,
    parse_version,
)
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


def _fake_git(monkeypatch, changed_files: list[str], base_version: str, commits: list[tuple[str, list[str]]] = ()):
    """A branch whose commits are (message, files) pairs; by default one commit of changed_files."""
    commits = list(commits) or [("Change things", changed_files)]
    shas = {f"commit{i}": commit for i, commit in enumerate(commits)}

    def git(*args: str) -> str:
        if args == ("rev-list", "base..HEAD"):
            return "\n".join(shas) + "\n"
        if args[:3] == ("show", "-s", "--format=%B"):
            return shas[args[3]][0] + "\n"
        if args[0] == "diff-tree":
            return "\n".join(shas[args[-1]][1]) + "\n"
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


def test_main_skips_a_commit_declaring_no_crawler_changes(monkeypatch):
    message = f"Add a server setting\n\n{NO_CRAWLER_CHANGES}\n\nCo-Authored-By: Someone"
    commits = [(message, [CRAWLER_FILE]), ("Change the API", ["mwmbl/platform/api.py"])]
    _fake_git(monkeypatch, [], CRAWLER_VERSION, commits)

    assert main("base") == 0


def test_main_still_counts_a_later_commit_to_the_same_file(monkeypatch):
    commits = [(NO_CRAWLER_CHANGES, [CRAWLER_FILE]), ("Change the crawler", [CRAWLER_FILE])]
    _fake_git(monkeypatch, [], CRAWLER_VERSION, commits)

    assert main("base") == 1


def test_the_declaration_must_be_a_line_of_its_own(monkeypatch):
    commits = [(f"Not true: {NO_CRAWLER_CHANGES}", [CRAWLER_FILE])]
    _fake_git(monkeypatch, [], CRAWLER_VERSION, commits)

    assert main("base") == 1


def test_a_clean_merge_like_cis_adds_no_changes_of_its_own(monkeypatch, tmp_path):
    """CI checks out a merge of the PR into its base, whose diff with the base is the whole PR."""

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout

    def commit(path: str, message: str) -> None:
        (tmp_path / path).write_text(message)
        git("add", path)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)

    git("init", "-q", "-b", "main")
    commit("base.py", "Base")
    git("checkout", "-q", "-b", "pr")
    commit(CRAWLER_FILE.replace("/", "_"), NO_CRAWLER_CHANGES)
    git("checkout", "-q", "main")
    commit("main.py", "Meanwhile on main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", "Merge pr into main", "pr")
    monkeypatch.setattr(release_check, "REPO_ROOT", tmp_path)

    assert release_check.files_changed_since("HEAD^1") == []


def _fake_staged(monkeypatch, staged_files: list[str], base_version: str, staged_version: str):
    def git(*args: str) -> str:
        if args == ("diff", "--cached", "--name-only"):
            return "\n".join(staged_files) + "\n"
        if args == ("merge-base", "HEAD", "origin/main"):
            return "base\n"
        if args == ("show", "base:mwmbl/crawler/version.py"):
            return f'CRAWLER_VERSION: str = "{base_version}"\n'
        assert args == ("show", ":mwmbl/crawler/version.py")
        return f'CRAWLER_VERSION: str = "{staged_version}"\n'

    monkeypatch.setattr(release_check, "_git", git)
    monkeypatch.setattr(release_check, "crawler_source_files", lambda: {CRAWLER_FILE})


def _message(tmp_path, text: str) -> str:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(text)
    return str(path)


def test_the_hook_fails_a_crawler_change_without_a_bump_or_declaration(monkeypatch, tmp_path, capsys):
    _fake_staged(monkeypatch, [CRAWLER_FILE], "1.0.0", "1.0.0")

    assert check_commit_message(_message(tmp_path, "Change the crawler\n")) == 1
    output = capsys.readouterr().out
    assert CRAWLER_FILE in output
    assert NO_CRAWLER_CHANGES in output


def test_the_hook_passes_a_declared_commit_without_looking_at_it(monkeypatch, tmp_path):
    monkeypatch.setattr(release_check, "_git", None)

    assert check_commit_message(_message(tmp_path, f"Add a setting\n\n{NO_CRAWLER_CHANGES}\n")) == 0


def test_the_hook_passes_once_the_branch_has_raised_the_version(monkeypatch, tmp_path):
    _fake_staged(monkeypatch, [CRAWLER_FILE], "1.0.0", "1.0.1")
    monkeypatch.setattr(release_check, "crawler_source_files", None)

    assert check_commit_message(_message(tmp_path, "Change the crawler\n")) == 0


def test_the_hook_skips_the_import_when_nothing_could_be_crawler_code(monkeypatch, tmp_path):
    _fake_staged(monkeypatch, ["README.md", "front-end/src/app.js", "test/test_x.py"], "1.0.0", "1.0.0")
    monkeypatch.setattr(release_check, "crawler_source_files", None)

    assert check_commit_message(_message(tmp_path, "Docs\n")) == 0


def test_the_hook_passes_server_only_changes(monkeypatch, tmp_path):
    _fake_staged(monkeypatch, ["mwmbl/platform/api.py"], "1.0.0", "1.0.0")

    assert check_commit_message(_message(tmp_path, "Change the API\n")) == 0
