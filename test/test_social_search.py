"""Searching Mwmbl Social's posts (mwmbl.social.search_posts, GET /api/v2/social/search), with its
search API faked at the HTTP boundary by pytest-httpx."""

import httpx
import pytest
from django.core.cache import cache

from mwmbl import social

SEARCH = "/api/v2/social/search"
API = "https://social.test/api/v1/search/posts"
POST = {
    "uri": "https://mastodon.example/users/alice/statuses/1",
    "author_uri": "https://mastodon.example/users/alice",
    "extract": "Garlic planted today #gardening",
    "language": "en",
    "published": 1763337600,
    "checked": 1763337700,
    "score": 1.5,
}


@pytest.fixture(autouse=True)
def empty_cache():
    cache.clear()
    yield
    cache.clear()


def search(client, q: str, **params):
    return client.get(SEARCH, {"q": q, **params}, headers={"X-Forwarded-For": "1.2.3.4"})


def test_anyone_can_search_and_gets_typed_posts(client, db, httpx_mock):
    lemmy_post = POST | {
        "uri": "https://lemmy.example/post/9",
        "author_uri": "https://lemmy.example/u/bob",
        "title": "Tips",
    }
    httpx_mock.add_response(url=f"{API}?q=garlic+bread&limit=20", json=[POST, lemmy_post])

    response = search(client, " garlic bread ")

    assert response.status_code == 200
    assert response.json() == [
        {
            "url": POST["uri"],
            "author_uri": POST["author_uri"],
            "author_handle": "@alice@mastodon.example",
            "title": None,
            "snippet": "Garlic planted today #gardening",
            "published": "2025-11-17T00:00:00Z",
        },
        {
            "url": "https://lemmy.example/post/9",
            "author_uri": "https://lemmy.example/u/bob",
            "author_handle": "@bob@lemmy.example",
            "title": "Tips",
            "snippet": "Garlic planted today #gardening",
            "published": "2025-11-17T00:00:00Z",
        },
    ]


@pytest.mark.parametrize(
    "author_uri, handle",
    [
        ("https://mastodon.example/users/alice", "@alice@mastodon.example"),
        ("https://mastodon.example/@alice", "@alice@mastodon.example"),
        ("https://lemmy.example/u/bob", "@bob@lemmy.example"),
        ("https://misskey.example/users/9abc/", "@9abc@misskey.example"),
        ("https://other.example/profile/carol", None),
        ("javascript://x/@alice", None),
        ("https://evil.example/u/Mwmbl%20Official", None),
        ("https://xn--mastdon-djg.social/@alice", "@alice@xn--mastdon-djg.social"),
    ],
)
def test_the_author_has_a_handle_where_the_address_shows_one(author_uri, handle):
    assert social.author_handle(author_uri) == handle


def test_only_http_addresses_are_returned_and_text_is_passed_on_as_text(client, db, httpx_mock):
    hostile = {
        "uri": "javascript:alert(1)",
        "author_uri": "https:///users/alice",
        "title": '<img src=x onerror="alert(3)">',
        "extract": "<script>alert(4)</script>",
        "published": 1763337600,
    }
    httpx_mock.add_response(url=f"{API}?q=x&limit=20", json=[hostile])

    [post] = search(client, "x").json()

    assert (post["url"], post["author_uri"], post["author_handle"]) == (None, None, None)
    assert (post["title"], post["snippet"]) == (hostile["title"], hostile["extract"])


def test_results_are_cached_briefly_per_query_and_limit(client, db, httpx_mock, settings):
    httpx_mock.add_response(url=f"{API}?q=garlic&limit=20", json=[POST])
    httpx_mock.add_response(url=f"{API}?q=garlic&limit=5", json=[])

    first = search(client, "garlic").json()
    again = search(client, "garlic").json()
    search(client, "garlic", limit=5)

    assert first == again
    assert len(httpx_mock.get_requests()) == 2
    assert settings.SOCIAL_SEARCH_CACHE_SECONDS <= 10 * 60


@pytest.mark.parametrize(
    "fault",
    [
        {"exception": httpx.ReadTimeout("timed out")},
        {"exception": httpx.ConnectError("refused")},
        {"status_code": 500, "json": {"error": "the search failed"}},
    ],
)
def test_an_unavailable_search_says_so_and_is_not_cached(client, db, httpx_mock, fault):
    if "exception" in fault:
        httpx_mock.add_exception(fault["exception"], url=f"{API}?q=garlic&limit=20")
    else:
        httpx_mock.add_response(url=f"{API}?q=garlic&limit=20", **fault)
    httpx_mock.add_response(url=f"{API}?q=garlic&limit=20", json=[POST])

    failed = search(client, "garlic")

    assert (failed.status_code, failed.json()["code"]) == (503, "social_unavailable")
    assert len(search(client, "garlic").json()) == 1


def test_no_query_asks_nothing(client, db, httpx_mock):
    response = search(client, "  ")

    assert (response.status_code, response.json()) == (200, [])
    assert not httpx_mock.get_requests()


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"q": "x" * 201}])
def test_out_of_range_parameters_are_refused(client, db, params):
    assert search(client, **({"q": "garlic"} | params)).status_code == 422


def test_each_network_has_a_per_minute_limit(client, db, httpx_mock, settings):
    settings.SOCIAL_SEARCH_RATE_LIMIT_PER_MINUTE = 1
    httpx_mock.add_response(url=f"{API}?q=garlic&limit=20", json=[POST])

    search(client, "garlic")
    refused = search(client, "garlic")

    assert (refused.status_code, refused.json()["code"]) == (429, "search_limit")
    assert 0 < refused.json()["retry_after"] <= 60
    assert refused["Retry-After"] == str(refused.json()["retry_after"])
