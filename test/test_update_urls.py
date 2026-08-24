from collections import defaultdict

from mwmbl.crawler.batch import Link
from mwmbl.indexer.update_urls import process_link
from mwmbl.indexer.blacklist_providers import CombinedBlacklistProvider, StaticBlacklistProvider


def test_process_link_normal():
    url_scores = {"https://somesite.com/something.html": 0.0, "https://somesite.com/": 0.0}
    url_timestamps = {}
    url_users = {}
    domain_links = defaultdict(set)
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
    )

    assert domain_links == {"somewhere.com": {"somesite.com"}}


def test_process_link_excludes_porn():
    url_scores = {}
    url_timestamps = {}
    url_users = {}
    domain_links = {}
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
    )

    assert url_scores == {}
    assert url_timestamps == {}
    assert url_users == {}
    assert domain_links == {}


def test_process_link_keeps_a_curated_domain():
    """The blocklists carry false positives that are wrong for a search index, so a domain
    a moderator has approved must survive link discovery. This is the crawler's path: it
    takes the approved names from the URL queue, which fetches them over HTTP."""
    url_timestamps = {}
    url_users = {}
    domain_links = defaultdict(set)
    blacklist_provider = CombinedBlacklistProvider(
        [StaticBlacklistProvider({"pudding.cool"})],
        get_exempt_domains=lambda: {"pudding.cool"},
    )

    process_link(
        user_id_hash="abc123",
        crawled_page_domain="somewhere.com",
        link=Link(url="https://pudding.cool/2025/12/motifs", link_type="content"),
        timestamp=1234,
        url_timestamps=url_timestamps,
        url_users=url_users,
        blacklist_provider=blacklist_provider,
        domain_links=domain_links,
    )

    assert domain_links == {"somewhere.com": {"pudding.cool"}}
