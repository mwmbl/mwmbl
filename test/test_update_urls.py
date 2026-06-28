from collections import defaultdict

import pytest

from mwmbl.crawler.batch import Link
from mwmbl.indexer.update_urls import process_link, _propagate_source_provenance
from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.models import SourceProvenance


def test_process_link_normal():
    url_timestamps = {}
    url_users = {}
    domain_links = defaultdict(set)
    crawled_page_links = {}
    blacklist_provider = StaticBlacklistProvider(set())

    process_link(
        user_id_hash="abc123",
        crawled_page_domain="somewhere.com",
        link=Link(url="https://somesite.com/something.html", link_type="nav"),
        timestamp=1234,
        url_timestamps=url_timestamps,
        url_users=url_users,
        blacklist_provider=blacklist_provider,
        domain_links=domain_links,
        page_url="https://somewhere.com/page.html",
        crawled_page_links=crawled_page_links,
    )

    assert domain_links == {"somewhere.com": {"somesite.com"}}
    # The accepted link is remembered against the page it was found on.
    assert crawled_page_links == {
        "https://somewhere.com/page.html": ["https://somesite.com/something.html"]
    }


def test_process_link_excludes_porn():
    url_timestamps = {}
    url_users = {}
    domain_links = {}
    crawled_page_links = {}
    # Create a blacklist provider that blocks porn sites
    blacklist_provider = StaticBlacklistProvider({"somepornsite.com"})

    process_link(
        user_id_hash="abc123",
        crawled_page_domain="somewhere.com",
        link=Link(url="https://somepornsite.com/something.html", link_type="content"),
        timestamp=1234,
        url_timestamps=url_timestamps,
        url_users=url_users,
        blacklist_provider=blacklist_provider,
        domain_links=domain_links,
        page_url="https://somewhere.com/page.html",
        crawled_page_links=crawled_page_links,
    )

    assert url_timestamps == {}
    assert url_users == {}
    assert domain_links == {}
    # Blacklisted links are not recorded for provenance propagation either.
    assert crawled_page_links == {}


PAGE = "https://gov.uk/guidance"
CHILD_1 = "https://gov.uk/guidance/details"
CHILD_2 = "https://example.com/referenced"


def test_propagate_source_provenance_noop_without_database(monkeypatch):
    monkeypatch.setattr("django.conf.settings.HAS_DATABASE", False)
    # Should not raise even though provenance is gated off.
    _propagate_source_provenance({PAGE: [CHILD_1]})


@pytest.mark.django_db
def test_propagate_source_provenance_carries_source_to_links(monkeypatch):
    monkeypatch.setattr("django.conf.settings.HAS_DATABASE", True)
    SourceProvenance.objects.create(url=PAGE, source="gov.uk", query="benefits", depth=0)

    _propagate_source_provenance({PAGE: [CHILD_1, CHILD_2]})

    for child in (CHILD_1, CHILD_2):
        row = SourceProvenance.objects.get(url=child)
        assert row.source == "gov.uk"
        assert row.parent_url == PAGE
        assert row.depth == 1
        assert row.query is None


@pytest.mark.django_db
def test_propagate_source_provenance_skips_unknown_pages(monkeypatch):
    monkeypatch.setattr("django.conf.settings.HAS_DATABASE", True)
    # PAGE has no provenance row, so nothing should be written for its links.
    _propagate_source_provenance({PAGE: [CHILD_1, CHILD_2]})
    assert SourceProvenance.objects.count() == 0


@pytest.mark.django_db
def test_propagate_source_provenance_respects_depth_cap(monkeypatch):
    monkeypatch.setattr("django.conf.settings.HAS_DATABASE", True)
    monkeypatch.setattr("django.conf.settings.SOURCE_PROVENANCE_MAX_DEPTH", 2)
    SourceProvenance.objects.create(url=PAGE, source="gov.uk", depth=2)

    _propagate_source_provenance({PAGE: [CHILD_1]})

    assert not SourceProvenance.objects.filter(url=CHILD_1).exists()
