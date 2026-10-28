"""Tests for the CI check that crawler changes raise CRAWLER_VERSION."""

import pytest

from mwmbl.crawler.release_check import changed_crawler_files, crawler_source_files, parse_version


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
