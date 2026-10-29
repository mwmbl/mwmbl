"""The search index is created explicitly - by manage.py create_index or the crawler - never on startup."""

from pathlib import Path

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import BaseCommand

from mwmbl import crawl
from mwmbl.management.commands.create_index import Command
from mwmbl.tinysearchengine.indexer import Document, TinyIndex


def test_ready_creates_the_external_cache_but_not_the_search_index(settings, tmp_path):
    settings.DATA_PATH = str(tmp_path)

    apps.get_app_config("mwmbl").ready()

    assert not (tmp_path / settings.INDEX_NAME).exists()
    assert (tmp_path / settings.EXTERNAL_CACHE_INDEX_NAME).exists()


def test_create_index_command_creates_an_index_of_the_configured_size(settings, tmp_path):
    settings.DATA_PATH = str(tmp_path)

    call_command("create_index")

    index = TinyIndex(item_factory=Document, index_path=tmp_path / settings.INDEX_NAME)
    assert index.num_pages == settings.NUM_PAGES


def test_create_index_command_skips_system_checks(settings, tmp_path, monkeypatch):
    # The checks import search_setup via the URLconf, which fails when there is no index yet.
    settings.DATA_PATH = str(tmp_path)

    def fail_checks(*args, **kwargs):
        raise AssertionError("create_index must not run the system checks")

    monkeypatch.setattr(BaseCommand, "check", fail_checks)

    # Run it as manage.py does: call_command skips the checks by default.
    Command().run_from_argv(["manage.py", "create_index"])

    assert (tmp_path / settings.INDEX_NAME).exists()


def test_crawler_main_creates_its_index(settings, tmp_path, monkeypatch):
    settings.DATA_PATH = str(tmp_path)
    monkeypatch.setattr("sys.argv", ["mwmbl-crawl"])
    created_before_run = []
    monkeypatch.setattr(crawl, "run", lambda: created_before_run.append(Path(tmp_path / settings.INDEX_NAME).exists()))

    crawl.main()

    assert created_before_run == [True]
