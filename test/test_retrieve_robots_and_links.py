"""
robots.txt handling and its Redis cache, link extraction, and the error results crawl_url
returns instead of raising.
"""

import json
import time

import fakeredis
import pytest

from mwmbl.crawler import retrieve
from mwmbl.crawler.retrieve import (
    ROBOTS_CACHE_ERROR_TTL_SECONDS,
    ROBOTS_CACHE_TTL_SECONDS,
    RobotsUnavailable,
    crawl_counting_overlap,
    crawl_url,
    get_dom_links,
    get_new_links,
    record_overlap,
    robots_allowed,
    robots_crawl_delay,
)
from mwmbl.justext.core import Paragraph, PathInfo, html_to_dom

URL = "https://example.test/private/page"
ROBOTS = b"User-agent: *\nDisallow: /private/\n"


@pytest.fixture
def redis():
    return fakeredis.FakeStrictRedis()


def _fetching(monkeypatch, status: int, content: bytes):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return status, content, url

    monkeypatch.setattr(retrieve, "fetch", fake_fetch)
    return calls


def test_robots_disallow_is_obeyed_and_cached(monkeypatch, redis):
    calls = _fetching(monkeypatch, 200, ROBOTS)

    first = robots_allowed(URL, redis)
    second = robots_allowed(URL, redis)

    assert first is False and second is False
    assert calls == ["https://example.test/robots.txt"]
    assert ROBOTS_CACHE_TTL_SECONDS - 5 <= redis.ttl("robots:example.test") <= ROBOTS_CACHE_TTL_SECONDS


def test_other_paths_stay_allowed(monkeypatch, redis):
    _fetching(monkeypatch, 200, ROBOTS)

    assert robots_allowed("https://example.test/public", redis) is True


def test_unparseable_url_is_not_allowed(redis):
    assert robots_allowed("http://[bad", redis) is False


def test_missing_robots_allows_everything_and_is_cached_briefly(monkeypatch, redis):
    _fetching(monkeypatch, 404, b"")

    assert robots_allowed(URL, redis) is True
    assert ROBOTS_CACHE_ERROR_TTL_SECONDS - 5 <= redis.ttl("robots:example.test") <= ROBOTS_CACHE_ERROR_TTL_SECONDS


def _unreachable(url):
    raise ConnectionError("refused")


@pytest.mark.parametrize("unreachable", ["refused", "server error"])
def test_unavailable_robots_disallows_everything_and_is_cached_briefly(monkeypatch, redis, unreachable):
    """RFC 9309: a robots.txt we cannot get, as opposed to one that is not there, disallows all."""
    if unreachable == "refused":
        monkeypatch.setattr(retrieve, "fetch", _unreachable)
    else:
        _fetching(monkeypatch, 503, b"")

    with pytest.raises(RobotsUnavailable):
        robots_allowed(URL, redis)
    assert ROBOTS_CACHE_ERROR_TTL_SECONDS - 5 <= redis.ttl("robots:example.test") <= ROBOTS_CACHE_ERROR_TTL_SECONDS

    calls = _fetching(monkeypatch, 200, b"")
    with pytest.raises(RobotsUnavailable):
        robots_allowed(URL, redis)
    assert calls == []


def test_crawl_url_reports_unavailable_robots_without_fetching_the_page(monkeypatch, redis):
    monkeypatch.setattr(retrieve, "fetch", _unreachable)

    result = crawl_url(URL, redis)

    assert result["error"]["name"] == "RobotsUnavailable"
    assert result["content"] is None


@pytest.mark.parametrize(("robots", "expected"), [(b"User-agent: *\nCrawl-delay: 30\n", 30), (ROBOTS, None)])
def test_crawl_delay_is_read_from_robots(monkeypatch, redis, robots, expected):
    _fetching(monkeypatch, 200, robots)

    assert robots_crawl_delay(URL, redis) == expected


def test_robots_in_latin_1_are_decoded(monkeypatch, redis):
    _fetching(monkeypatch, 200, "# caf\xe9\nUser-agent: *\nDisallow: /private/\n".encode("iso-8859-1"))

    assert robots_allowed(URL, redis) is False


def test_expired_cache_entry_is_refetched(monkeypatch, redis):
    expired = {"content": [], "cached_at": 0, "expires_at": int(time.time()) - 10}
    redis.set("robots:example.test", json.dumps(expired))
    calls = _fetching(monkeypatch, 200, ROBOTS)

    assert robots_allowed(URL, redis) is False
    assert len(calls) == 1


# The last is the shape entries had before they recorded whether robots.txt was unavailable.
@pytest.mark.parametrize(
    "entry",
    [
        "not json",
        json.dumps({"content": []}),
        json.dumps({"content": [], "cached_at": 0, "expires_at": int(time.time()) + 3600}),
    ],
)
def test_corrupt_cache_entry_is_dropped_and_refetched(monkeypatch, redis, entry):
    redis.set("robots:example.test", entry)
    calls = _fetching(monkeypatch, 200, ROBOTS)

    assert robots_allowed(URL, redis) is False
    assert len(calls) == 1


def _paragraph(links: list[str], class_type: str) -> Paragraph:
    paragraph = Paragraph(PathInfo())
    paragraph.links = links
    paragraph.class_type = class_type
    return paragraph


def test_good_paragraph_links_are_new_and_the_rest_extra():
    paragraphs = [
        _paragraph(["/a", "/skip.jpg", "mailto:someone@example.test"], "good"),
        _paragraph(["/b", "/a"], "bad"),
    ]

    new_links, extra_links = get_new_links(paragraphs, "https://example.test/")

    assert new_links == {"https://example.test/a"}
    assert extra_links == {"https://example.test/b"}


def test_link_collection_stops_once_both_sets_are_full(monkeypatch):
    monkeypatch.setattr(retrieve, "MAX_NEW_LINKS", 1)
    monkeypatch.setattr(retrieve, "MAX_EXTRA_LINKS", 1)
    paragraphs = [
        _paragraph(["/a", "/a2"], "good"),
        _paragraph(["/b", "/b2"], "bad"),
    ]

    new_links, extra_links = get_new_links(paragraphs, "https://example.test/")

    assert len(new_links) == 1
    assert len(extra_links) == 1


def test_invalid_ipv6_links_are_dropped_not_raised():
    paragraphs = [_paragraph(["http://[broken/", "/a"], "good")]

    new_links, extra_links = get_new_links(paragraphs, "https://example.test/")

    assert new_links == {"https://example.test/a"}
    assert extra_links == set()


def test_fragments_are_stripped_and_overlong_urls_dropped():
    dom = html_to_dom(
        (
            '<html><body><a href="/page#section"><img src="x.gif"></a>'
            '<a href="#top">top</a><a href=" ">blank</a>'
            f'<a href="/{"x" * 200}">long</a></body></html>'
        ).encode(),
        "utf8",
        None,
        "replace",
    )

    assert get_dom_links(dom, "https://example.test/") == {"https://example.test/page"}


def _error_name(result: dict) -> str:
    return result["error"]["name"]


def test_crawl_url_reports_robots_denial(monkeypatch, redis):
    monkeypatch.setattr(retrieve, "robots_allowed", lambda url, redis: False)

    result = crawl_url(URL, redis)

    assert _error_name(result) == "RobotsDenied"
    assert result["content"] is None


def test_crawl_url_reports_fetch_failures(monkeypatch, redis):
    def refuse(url):
        raise ConnectionError("refused")

    monkeypatch.setattr(retrieve, "robots_allowed", lambda url, redis: True)
    monkeypatch.setattr(retrieve, "fetch", refuse)

    result = crawl_url(URL, redis)

    assert _error_name(result) == "AbortError"
    assert result["error"]["message"] == "refused"


def test_crawl_url_reports_an_empty_response(monkeypatch, redis):
    monkeypatch.setattr(retrieve, "robots_allowed", lambda url, redis: True)
    _fetching(monkeypatch, 200, b"")

    assert _error_name(crawl_url(URL, redis)) == "NoResponseText"


def test_crawl_url_reports_unparseable_html(monkeypatch, redis):
    def broken(*args, **kwargs):
        raise RuntimeError("cannot parse")

    monkeypatch.setattr(retrieve, "robots_allowed", lambda url, redis: True)
    _fetching(monkeypatch, 200, b"<html></html>")
    monkeypatch.setattr(retrieve, "html_to_dom", broken)

    result = crawl_url(URL, redis)

    assert _error_name(result) == "RuntimeError"


def test_crawl_url_reports_paragraph_extraction_failures(monkeypatch, redis):
    def broken(*args, **kwargs):
        raise RuntimeError("cannot classify")

    monkeypatch.setattr(retrieve, "robots_allowed", lambda url, redis: True)
    _fetching(monkeypatch, 200, b"<html><body><p>Hello</p></body></html>\x01")
    monkeypatch.setattr(retrieve.core, "justext_from_dom", broken)

    assert _error_name(crawl_url(URL, redis)) == "RuntimeError"


def test_crawl_url_truncates_long_titles_and_extracts(monkeypatch, redis):
    sentence = "This is a perfectly ordinary sentence about the history of the town and its people. "
    html = f"<html><head><title>{'T' * 500}</title></head><body><p>{sentence * 60}</p></body></html>"
    monkeypatch.setattr(retrieve, "robots_allowed", lambda url, redis: True)
    _fetching(monkeypatch, 200, html.encode())

    content = crawl_url(URL, redis)["content"]

    assert len(content["title"]) == retrieve.NUM_TITLE_CHARS
    assert content["title"].endswith("…")
    assert content["extract"].endswith("…")


def test_overlap_counter_is_released_after_the_crawl(monkeypatch, redis):
    seen = []

    def fake_crawl(url, redis):
        seen.append(int(redis.get(retrieve.DOMAIN_IN_FLIGHT_KEY.format(domain="example.test"))))
        return {"url": url}

    monkeypatch.setattr(retrieve, "crawl_url", fake_crawl)

    result, overlap = crawl_counting_overlap(URL, redis)

    assert result == {"url": URL}
    assert overlap == 0
    assert seen == [1]
    assert int(redis.get(retrieve.DOMAIN_IN_FLIGHT_KEY.format(domain="example.test"))) == 0


def test_record_overlap_totals_accumulate(redis):
    urls = ["https://a.test/1", "https://a.test/2", "https://b.test/"]

    record_overlap(urls, [0, 1, 2], redis)
    record_overlap(urls, [0, 0, 0], redis)

    totals = {k.decode(): int(v) for k, v in redis.hgetall(retrieve.DOMAIN_OVERLAP_KEY).items()}
    assert totals == {"fetches": 6, "overlapped": 2, "duplicate_domains": 2}
