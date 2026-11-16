"""
robots.txt handling and its Redis cache, link extraction, and the error results crawl_url
returns instead of raising.
"""

import json
import time
from types import SimpleNamespace

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

    def fake_fetch(url, robots_redis=None):
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


@pytest.mark.parametrize("unreachable", ["refused", "server error", "rate limited"])
def test_unavailable_robots_disallows_everything_and_is_cached_briefly(monkeypatch, redis, unreachable):
    """RFC 9309: a robots.txt we cannot get, as opposed to one that is not there, disallows all."""
    if unreachable == "refused":
        monkeypatch.setattr(retrieve, "fetch", _unreachable)
    else:
        _fetching(monkeypatch, 503 if unreachable == "server error" else 429, b"")

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
    def refuse(url, robots_redis=None):
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


# robots.txt shaped like the one in #333: the rules for everyone are split across several
# "User-agent: *" groups, the first of which allows everything.
SPLIT_GROUPS_ROBOTS = b"""User-agent: *
Allow: /

# Don't index things that are too big
User-agent: *
Disallow: /*.tar.gz$

User-agent: GPTBot
User-agent: ClaudeBot
Disallow: /

# Tars and feathers for those who disrespect robots.txt
User-agent: *
Disallow: /bad-bot
"""


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/", True),
        ("/bad-bot", False),
        ("/files/big.tar.gz", False),
        ("/files/big.tar.gz.html", True),
    ],
)
def test_every_group_for_all_agents_is_obeyed(monkeypatch, redis, path, allowed):
    _fetching(monkeypatch, 200, SPLIT_GROUPS_ROBOTS)

    assert robots_allowed(f"https://example.test{path}", redis) is allowed


def test_a_group_naming_mwmbl_is_obeyed_over_the_one_for_all_agents(monkeypatch, redis):
    _fetching(monkeypatch, 200, b"User-agent: *\nAllow: /\n\nUser-agent: mwmbl\nDisallow: /\n")

    assert robots_allowed("https://example.test/page", redis) is False


def test_the_longest_matching_rule_wins_whatever_its_order(monkeypatch, redis):
    _fetching(monkeypatch, 200, b"User-agent: *\nAllow: /\nDisallow: /private/\n")

    assert robots_allowed(URL, redis) is False


def test_a_byte_order_mark_does_not_hide_the_first_group(monkeypatch, redis):
    _fetching(monkeypatch, 200, b"\xef\xbb\xbf" + ROBOTS)

    assert robots_allowed(URL, redis) is False


class _Response:
    def __init__(self, status_code: int, body: bytes = b"", location: str | None = None):
        self.status_code = status_code
        self.is_redirect = location is not None
        self.next = object() if self.is_redirect else None
        self.headers = {"Location": location} if location else {}
        self._body = body

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


def test_a_redirect_into_a_disallowed_path_is_not_followed(monkeypatch, redis):
    """Checking only the URL we were asked for let a redirect walk us into a disallowed path."""
    requested = []
    responses = {
        "http://93.184.216.34/robots.txt": _Response(200, b"User-agent: *\nDisallow: /bad-bot\n"),
        "http://93.184.216.34/start": _Response(301, location="/bad-bot"),
    }

    def fake_get(url, **kwargs):
        requested.append(url)
        return responses[url]

    monkeypatch.setattr(retrieve, "get_session", lambda: SimpleNamespace(get=fake_get))

    result = crawl_url("http://93.184.216.34/start", redis)

    assert _error_name(result) == "RobotsDenied"
    assert "http://93.184.216.34/bad-bot" not in requested


@pytest.mark.parametrize("word", ["github", "contact"])
def test_groups_match_our_name_not_other_words_in_the_user_agent(monkeypatch, redis, word):
    _fetching(monkeypatch, 200, f"User-agent: {word}\nDisallow: /\n\nUser-agent: mwmbl\nAllow: /\n".encode())

    assert robots_allowed(URL, redis) is True


def test_a_byte_order_mark_in_an_old_cache_entry_does_not_hide_the_first_group(redis):
    lines = ["﻿User-agent: *", "Disallow: /private/"]
    redis.set(
        "robots:example.test", json.dumps({"content": lines, "unavailable": False, "expires_at": time.time() + 60})
    )

    assert robots_allowed(URL, redis) is False


def test_a_fractional_crawl_delay_is_rounded_up(monkeypatch, redis):
    _fetching(monkeypatch, 200, b"User-agent: *\nCrawl-delay: 1.5\n")

    assert robots_crawl_delay(URL, redis) == 2


def test_a_redirect_to_another_scheme_does_not_poison_that_sites_robots_cache(monkeypatch, redis):
    def fake_get(url, **kwargs):
        if url.endswith("/robots.txt"):
            return _Response(404)
        return _Response(302, location="ftp://victim.test/")

    monkeypatch.setattr(retrieve, "get_session", lambda: SimpleNamespace(get=fake_get))

    result = crawl_url("http://93.184.216.34/start", redis)

    assert _error_name(result) == "AbortError"
    assert redis.get("robots:victim.test") is None


def test_an_allowed_redirect_to_another_host_is_followed(monkeypatch, redis):
    responses = {
        "http://93.184.216.34/robots.txt": _Response(404),
        "http://93.184.216.34/start": _Response(301, location="http://93.184.216.35/page"),
        "http://93.184.216.35/robots.txt": _Response(200, b"User-agent: *\nDisallow: /private/\n"),
        "http://93.184.216.35/page": _Response(200, b"<html><body><p>A page about something.</p></body></html>"),
    }
    monkeypatch.setattr(retrieve, "get_session", lambda: SimpleNamespace(get=lambda url, **kwargs: responses[url]))

    result = crawl_url("http://93.184.216.34/start", redis)

    assert result["error"] is None
    assert result["resolved_url"] == "http://93.184.216.35/page"
