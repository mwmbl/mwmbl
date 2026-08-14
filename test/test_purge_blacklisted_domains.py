from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.tinysearchengine.indexer import Document, PAGE_SIZE, TinyIndex


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "test.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=10, page_size=PAGE_SIZE)
    return str(path)


def _put(index_path, term, doc):
    with TinyIndex(item_factory=Document, index_path=index_path, mode="w") as index:
        page = index.get_key_page_index(term)
        existing = index.get_page(page)
        doc.term = term
        index.store_in_page(page, existing + [doc])


def _get_all_urls(index_path, num_pages=10):
    urls = []
    with TinyIndex(item_factory=Document, index_path=index_path, mode="r") as index:
        for page_index in range(num_pages):
            urls.extend(doc.url for doc in index.get_page(page_index))
    return urls


@pytest.mark.django_db
def test_purge_removes_blacklisted_domain_only(index_path):
    _put(index_path, "term", Document("Bad", "https://fineartteens.com/x", "extract"))
    _put(index_path, "term", Document("Good", "https://example.com/y", "extract"))

    with patch(
        "mwmbl.management.commands.purge_blacklisted_domains.get_default_blacklist_provider",
        return_value=StaticBlacklistProvider({"fineartteens.com"}),
    ), override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains")

    urls = _get_all_urls(index_path)
    assert "https://fineartteens.com/x" not in urls
    assert "https://example.com/y" in urls


@pytest.mark.django_db
def test_purge_dry_run_does_not_modify_index(index_path):
    _put(index_path, "term", Document("Bad", "https://fineartteens.com/x", "extract"))

    with patch(
        "mwmbl.management.commands.purge_blacklisted_domains.get_default_blacklist_provider",
        return_value=StaticBlacklistProvider({"fineartteens.com"}),
    ), override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains", "--dry-run")

    urls = _get_all_urls(index_path)
    assert "https://fineartteens.com/x" in urls
