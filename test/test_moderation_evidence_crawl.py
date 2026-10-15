"""
Crawling a submitted domain for moderation evidence: which follow-up pages are picked,
how redirects and failures are reported, and when the blacklist lookup is unavailable.
"""

import pytest

from mwmbl.crawler.batch import HashedBatch, Item, ItemContent
from mwmbl.indexer.index_batches import get_documents_from_batches
from mwmbl.models import DomainEvidence
from mwmbl.moderation import evidence
from mwmbl.moderation.evidence import _final_domain, _follow_up_urls, crawl_domain, page_texts


def _homepage(links=(), extra_links=()):
    return {"content": {"links": list(links), "extra_links": list(extra_links)}}


def test_follow_ups_stay_on_the_site_skip_boilerplate_and_prefer_shallow_paths():
    homepage = _homepage(
        links=[
            "https://example.com/blog/2024/deep/article",
            "https://example.com/about",
            "https://example.com/privacy-policy",
            "https://example.com/",
            "https://other.test/about",
        ],
        extra_links=["https://www.example.com/products"],
    )

    urls = _follow_up_urls("example.com", homepage, limit=5)

    assert urls == [
        "https://example.com/about",
        "https://www.example.com/products",
        "https://example.com/blog/2024/deep/article",
    ]


def test_follow_ups_are_capped_and_nothing_is_picked_for_a_zero_limit():
    homepage = _homepage(links=["https://example.com/a", "https://example.com/b"])

    assert len(_follow_up_urls("example.com", homepage, limit=1)) == 1
    assert _follow_up_urls("example.com", homepage, limit=0) == []
    assert _follow_up_urls("example.com", {"content": None}, limit=3) == []


@pytest.mark.parametrize(
    "resolved_url, expected",
    [
        ("https://www.example.com/home", "www.example.com"),
        ("https://example.com/home", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_final_domain_is_only_reported_after_a_redirect(resolved_url, expected):
    assert _final_domain({"resolved_url": resolved_url}, "example.com") == expected


def _crawled(url: str, links=()):
    return {
        "url": url,
        "resolved_url": url,
        "status": 200,
        "content": {"title": "T", "extract": "E", "links": list(links), "extra_links": []},
        "error": None,
    }


def test_crawling_a_domain_fetches_the_homepage_then_its_follow_up_pages(monkeypatch):
    fetched = []

    def fake_crawl(url, redis):
        fetched.append(url)
        links = ["https://example.com/about", "https://example.com/shop"] if url == "https://example.com/" else []
        return _crawled(url, links)

    monkeypatch.setattr(evidence, "crawl_url", fake_crawl)
    monkeypatch.setattr(evidence, "CRAWL_DELAY_SECONDS", 0)

    result = crawl_domain("example.com", redis=None, max_pages=3)

    assert fetched == ["https://example.com/", "https://example.com/shop", "https://example.com/about"]
    assert result["http_status"] == 200
    assert result["error"] == ""
    assert len(result["pages"]) == 3
    assert result["signals"]["https"] is True


def test_the_delay_between_pages_is_honoured(monkeypatch):
    sleeps = []

    def fake_crawl(url, redis):
        return _crawled(url, ["https://example.com/about"])

    monkeypatch.setattr(evidence, "crawl_url", fake_crawl)
    monkeypatch.setattr(evidence, "CRAWL_DELAY_SECONDS", 2)
    monkeypatch.setattr(evidence.time, "sleep", sleeps.append)

    crawl_domain("example.com", redis=None, max_pages=2)

    assert sleeps == [2]


def test_an_unreachable_domain_reports_the_error_rather_than_raising(monkeypatch):
    failure = {"url": "x", "status": None, "content": None, "error": {"name": "AbortError", "message": "down"}}
    monkeypatch.setattr(evidence, "crawl_url", lambda url, redis: failure)

    result = crawl_domain("down.example", redis=None, max_pages=3)

    assert result["error"] == "AbortError"
    assert result["signals"]["https"] is None
    assert result["signals"]["num_pages_fetched"] == 0


def test_a_missing_blacklist_snapshot_is_one_missing_signal_not_a_failure(monkeypatch):
    def unavailable():
        raise ConnectionError("redis not ready")

    monkeypatch.setattr(evidence, "crawl_url", lambda url, redis: _crawled(url))
    monkeypatch.setattr(evidence, "get_snapshot_blacklist", unavailable)

    result = crawl_domain("example.com", redis=None, max_pages=1)

    assert result["signals"]["blacklisted"] is False


def test_page_texts_join_title_and_extract():
    assert page_texts(None) == []
    row = DomainEvidence(domain="a.example", pages=[{"title": "Home", "extract": "Welcome"}, {"extract": "Only text"}])
    assert page_texts(row) == ["Home Welcome", "Only text"]
    assert page_texts(DomainEvidence(domain="b.example", pages=None)) == []


def test_only_crawled_pages_with_content_become_documents():
    batch = HashedBatch(
        user_id_hash="h",
        timestamp=1.0,
        items=[
            Item(
                url="https://a.example/",
                status=200,
                timestamp=1704067200000.0,
                content=ItemContent(title="A", extract="Extract A"),
            ),
            Item(
                url="https://b.example/",
                status=200,
                timestamp=1704067200000.0,
                content=ItemContent(title="B", extract="x", links_only=True),
            ),
            Item(url="https://c.example/", status=404, timestamp=1704067200000.0),
        ],
    )

    documents = list(get_documents_from_batches([batch]))

    assert [(d.title, d.url, d.last_crawled) for d in documents] == [("A", "https://a.example/", 1704067200)]
