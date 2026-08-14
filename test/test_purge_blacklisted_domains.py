from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.index_batches import index_documents
from mwmbl.tinysearchengine.indexer import Document, PAGE_SIZE, TinyIndex

PATCH_TARGET = "mwmbl.management.commands.purge_blacklisted_domains.get_default_blacklist_provider"


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "test.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=10, page_size=PAGE_SIZE)
    return str(path)


def _put(index_path, term, doc):
    """Force a document onto the page for a specific term, bypassing real tokenization."""
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


def _write_csv(tmp_path, *queries):
    path = tmp_path / "queries.csv"
    lines = ["Top queries,Clicks,Impressions,CTR,Position"]
    lines += [f"{q},0,1,0%,1" for q in queries]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


@pytest.mark.django_db
def test_purge_requires_a_mode(index_path):
    with override_settings(DATA_PATH="", INDEX_NAME=index_path), pytest.raises(ValueError):
        call_command("purge_blacklisted_domains")


@pytest.mark.django_db
def test_full_scan_removes_blacklisted_domain_only(index_path):
    _put(index_path, "term", Document("Bad", "https://fineartteens.com/x", "extract"))
    _put(index_path, "term", Document("Good", "https://example.com/y", "extract"))

    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider({"fineartteens.com"})), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains", "--full-scan")

    urls = _get_all_urls(index_path)
    assert "https://fineartteens.com/x" not in urls
    assert "https://example.com/y" in urls


@pytest.mark.django_db
def test_full_scan_dry_run_does_not_modify_index(index_path):
    _put(index_path, "term", Document("Bad", "https://fineartteens.com/x", "extract"))

    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider({"fineartteens.com"})), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains", "--full-scan", "--dry-run")

    assert "https://fineartteens.com/x" in _get_all_urls(index_path)


@pytest.mark.django_db
def test_targeted_domain_finds_via_guessed_terms(index_path):
    """With --domain and no --term, the domain's own name (run through the real tokenizer,
    the same way the indexer would have derived tokens from the URL) is enough to find and
    remove every page it was actually filed under."""
    documents = [
        Document(title="Fine Art Teens - galleries", url="https://fineartteens.com/x", extract="teen gallery photos"),
        Document(title="Good Example", url="https://example.com/y", extract="a good example page"),
    ]
    index_documents(documents, index_path)

    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider(set())), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains", "--domain", "fineartteens.com")

    urls = _get_all_urls(index_path)
    assert "https://fineartteens.com/x" not in urls
    assert "https://example.com/y" in urls


@pytest.mark.django_db
def test_targeted_domain_dry_run_does_not_modify_index(index_path):
    documents = [Document(title="Fine Art Teens", url="https://fineartteens.com/x", extract="teen gallery")]
    index_documents(documents, index_path)

    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider(set())), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains", "--domain", "fineartteens.com", "--dry-run")

    assert "https://fineartteens.com/x" in _get_all_urls(index_path)


@pytest.mark.django_db
def test_targeted_extra_term_reaches_pages_domain_guess_alone_would_miss(index_path):
    """A --term seed reaches a page the domain-guessed terms alone wouldn't hit,
    e.g. a term that only appears in the title/extract, not the URL."""
    documents = [
        Document(title="Kusowanka - Hentai Posts, XXX Toons", url="https://kusowanka.com/", extract="anime porn"),
    ]
    index_documents(documents, index_path)

    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider(set())), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains", "--domain", "kusowanka.com", "--term", "hentai")

    assert "https://kusowanka.com/" not in _get_all_urls(index_path)


@pytest.mark.django_db
def test_csv_auto_detects_blacklisted_domain_from_query_results(index_path, tmp_path):
    """The primary workflow: feed in a Search Console query export, and any indexed result
    for those queries that's blacklisted (checked against the real, full blacklist config -
    no manual domain list needed) gets purged from every page it's filed under."""
    documents = [
        Document(title="Fine Art Teens - galleries", url="https://fineartteens.com/x", extract="teen gallery photos"),
        Document(title="Good Example", url="https://example.com/y", extract="fineartteens is not this site"),
    ]
    index_documents(documents, index_path)
    csv_path = _write_csv(tmp_path, "fineartteens", "unrelated query with no results")

    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider({"fineartteens.com"})), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains", "--csv", csv_path)

    urls = _get_all_urls(index_path)
    assert "https://fineartteens.com/x" not in urls
    assert "https://example.com/y" in urls


@pytest.mark.django_db
def test_csv_with_no_matching_blacklisted_domains_changes_nothing(index_path, tmp_path):
    documents = [Document(title="Good Example", url="https://example.com/y", extract="a good example page")]
    index_documents(documents, index_path)
    csv_path = _write_csv(tmp_path, "good example")

    with patch(PATCH_TARGET, return_value=StaticBlacklistProvider(set())), \
            override_settings(DATA_PATH="", INDEX_NAME=index_path):
        call_command("purge_blacklisted_domains", "--csv", csv_path)

    assert "https://example.com/y" in _get_all_urls(index_path)
