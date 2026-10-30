"""
Recording the URLs found in crawled batches: which links are kept, who gets credit
for them, and what is queued.
"""

from collections import defaultdict
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from mwmbl.crawler.batch import HashedBatch, Item, ItemContent, ItemError, Link
from mwmbl.crawler.urls import URLStatus
from mwmbl.indexer import update_urls
from mwmbl.indexer.blacklist_providers import BlacklistProvider
from mwmbl.indexer.update_urls import get_datetime_from_timestamp, process_link, record_urls_in_database

TIMESTAMP = datetime(2024, 1, 1, tzinfo=timezone.utc)


class FakeBlacklist(BlacklistProvider):
    def is_domain_blacklisted(self, domain: str) -> bool:
        return domain == "blocked.example"


def _process(link_url: str):
    url_users, url_timestamps = {}, {}
    domain_links = defaultdict(set)
    process_link(
        "user", "source.example", Link(url=link_url, link_type="content"), TIMESTAMP,
        url_timestamps, url_users, FakeBlacklist(), domain_links,
    )  # fmt: skip
    return url_users, url_timestamps, domain_links


def test_timestamp_is_converted_to_an_aware_datetime():
    assert get_datetime_from_timestamp(1704067200.0) == TIMESTAMP


def test_a_link_credits_the_user_and_its_root_url():
    url_users, url_timestamps, domain_links = _process("https://target.example/page")

    assert url_users == {"https://target.example/page": "user", "https://target.example/": "user"}
    assert set(url_timestamps) == set(url_users)
    assert domain_links == {"source.example": {"target.example"}}


@pytest.mark.parametrize("link_url", ["https://blocked.example/page", "not a url"])
def test_unparseable_and_blacklisted_links_are_dropped(link_url):
    url_users, url_timestamps, domain_links = _process(link_url)

    assert url_users == {}
    assert url_timestamps == {}
    assert domain_links == {}


@pytest.fixture
def recorded(monkeypatch):
    """Patch the database, returning the URLs that would have been queued."""
    found_urls = []
    domain_link_db = MagicMock()
    domain_link_db.__enter__.return_value = domain_link_db

    monkeypatch.setattr(update_urls, "_add_found_urls_to_db_and_queue", lambda urls, queue: found_urls.extend(urls))
    monkeypatch.setattr(update_urls, "DomainLinkDatabase", lambda: domain_link_db)
    return found_urls, domain_link_db


def _item(url, content=None, status=200, error=None):
    return Item(url=url, status=status, timestamp=1704067200000.0, content=content, error=error)


def test_recording_a_batch_classifies_every_url(recorded):
    found_urls, domain_link_db = recorded
    content = ItemContent(title="T", extract="E", links=["https://linked.example/a"])
    batch = HashedBatch(
        user_id_hash="hash",
        timestamp=1704067200.0,
        items=[
            _item("https://ok.example/", content=content),
            _item("https://gone.example/", status=404),
            _item("https://slow.example/", error=ItemError(name="AbortError")),
            _item("https://robots.example/", error=ItemError(name="RobotsDenied")),
            _item("https://odd.example/", status=500),
            # A challenge page has content, but neither it nor its links are the site's.
            _item(
                "https://challenge.example/",
                status=403,
                content=ItemContent(title="Just a moment...", extract="", links=["https://challenge.example/help"]),
            ),
            _item("https://flaky.example/", error=ItemError(name="RobotsUnavailable")),
        ],
    )

    record_urls_in_database([batch], MagicMock(), FakeBlacklist())

    statuses = {found.url: found.status for found in found_urls}
    assert statuses == {
        "https://ok.example/": URLStatus.CRAWLED,
        "https://gone.example/": URLStatus.ERROR_404,
        "https://slow.example/": URLStatus.ERROR_TIMEOUT,
        "https://robots.example/": URLStatus.ERROR_ROBOTS_DENIED,
        "https://odd.example/": URLStatus.ERROR_OTHER,
        "https://challenge.example/": URLStatus.ERROR_OTHER,
        "https://flaky.example/": URLStatus.ERROR_TIMEOUT,
        "https://linked.example/a": URLStatus.NEW,
        "https://linked.example/": URLStatus.NEW,
    }
    assert {found.user_id_hash for found in found_urls} == {"hash"}
    domain_link_db.update_domain_links.assert_called_once_with("ok.example", {"linked.example"})


def test_a_crawled_page_with_an_unparseable_url_contributes_no_links(recorded):
    found_urls, domain_link_db = recorded
    content = ItemContent(title="T", extract="E", links=["https://linked.example/a"])
    batch = HashedBatch(user_id_hash="hash", timestamp=1704067200.0, items=[_item("no-scheme", content=content)])

    record_urls_in_database([batch], MagicMock(), FakeBlacklist())

    assert [found.url for found in found_urls] == ["no-scheme"]
    domain_link_db.update_domain_links.assert_not_called()


def test_hn_links_are_added_until_the_domain_limit(monkeypatch):
    queue = MagicMock()
    queue.get_domain_count.side_effect = [0, 100, 200]
    monkeypatch.setattr(update_urls, "get_domain_max_urls", lambda domain, curated: 150)
    monkeypatch.setattr(update_urls.requests, "get", lambda url: MagicMock(json=lambda: 1000))
    added = []
    monkeypatch.setattr(update_urls, "_add_found_urls_to_db_and_queue", lambda urls, q: added.append(urls))

    update_urls.add_hn_links(queue)

    assert len(added) == 2
    assert added[0][0].url == "https://news.ycombinator.com/item?id=1000"
    assert added[1][0].url == "https://news.ycombinator.com/item?id=900"
