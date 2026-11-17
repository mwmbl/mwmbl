"""The social search page (mwmbl.social.search_posts, mwmbl.social_views.search), with Mwmbl
Social's search API faked at the HTTP boundary by pytest-httpx."""

from datetime import UTC, datetime

import httpx
import pytest
from django.core.cache import cache

from mwmbl import social

SEARCH = "/social/search/"
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


def test_posts_are_typed_and_asked_for_by_query(httpx_mock):
    httpx_mock.add_response(url=f"{API}?q=garlic+bread", json=[POST])

    assert social.search_posts("garlic bread") == [
        social.SocialPost(
            url="https://mastodon.example/users/alice/statuses/1",
            author_url="https://mastodon.example/users/alice",
            author="@alice@mastodon.example",
            title=None,
            extract="Garlic planted today #gardening",
            published=datetime(2025, 11, 17, tzinfo=UTC),
        )
    ]


@pytest.mark.parametrize(
    "author_uri, name",
    [
        ("https://mastodon.example/users/alice", "@alice@mastodon.example"),
        ("https://mastodon.example/@alice", "@alice@mastodon.example"),
        ("https://lemmy.example/u/bob", "@bob@lemmy.example"),
        ("https://misskey.example/users/9abc/", "@9abc@misskey.example"),
        ("https://other.example/profile/carol", "other.example/profile/carol"),
    ],
)
def test_the_author_is_named_by_handle_where_the_address_shows_one(author_uri, name):
    assert social.author_name(author_uri) == name


def test_results_are_cached_briefly_per_query(httpx_mock, settings):
    httpx_mock.add_response(url=f"{API}?q=garlic", json=[POST])
    httpx_mock.add_response(url=f"{API}?q=onion", json=[])

    social.search_posts("garlic")
    cached = social.search_posts("garlic")
    social.search_posts("onion")

    assert cached[0].extract == POST["extract"]
    assert len(httpx_mock.get_requests()) == 2
    assert settings.SOCIAL_SEARCH_CACHE_SECONDS <= 10 * 60


def test_the_page_shows_each_post_with_author_date_and_links(client, httpx_mock):
    lemmy_post = POST | {
        "uri": "https://lemmy.example/post/9",
        "author_uri": "https://lemmy.example/u/bob",
        "title": "Allotment tips",
    }
    httpx_mock.add_response(url=f"{API}?q=garlic", json=[POST, lemmy_post])

    response = client.get(SEARCH, {"q": " garlic "})

    page = response.content.decode()
    assert response.status_code == 200
    assert response["X-Robots-Tag"] == "noindex"
    assert f'<a href="{POST["author_uri"]}" rel="ugc nofollow">@alice@mastodon.example</a>' in page
    assert '<time datetime="2025-11-17T00:00:00+00:00">17 Nov 2025</time>' in page
    assert f'<a href="{POST["uri"]}" rel="ugc nofollow">View the original post</a>' in page
    assert "Garlic planted today #gardening" in page
    assert "Allotment tips" in page
    assert "@bob@lemmy.example" in page
    assert 'href="/social/privacy/"' in page


def test_hostile_content_is_escaped_and_only_http_addresses_become_links(client, httpx_mock):
    hostile = {
        "uri": "javascript:alert(1)",
        "author_uri": "javascript:alert(2)//<script>",
        "title": '<img src=x onerror="alert(3)">',
        "extract": "<script>alert(4)</script><a href='https://evil.example'>x</a>",
        "published": 1763337600,
    }
    httpx_mock.add_response(url=f"{API}?q=x", json=[hostile])

    page = client.get(SEARCH, {"q": "x"}).content.decode()

    assert 'href="javascript:' not in page
    assert "<script>alert" not in page
    assert "<img src=x" not in page
    assert "<a href='https://evil.example'>" not in page
    assert "&lt;script&gt;alert(4)&lt;/script&gt;" in page
    assert "javascript:alert(2)//&lt;script&gt;" in page
    assert "View the original post" not in page


@pytest.mark.parametrize(
    "fault",
    [
        {"exception": httpx.ReadTimeout("timed out")},
        {"exception": httpx.ConnectError("refused")},
        {"status_code": 500, "json": {"error": "the search failed"}},
    ],
)
def test_an_unavailable_search_says_so(client, httpx_mock, fault):
    if "exception" in fault:
        httpx_mock.add_exception(fault["exception"], url=f"{API}?q=garlic")
    else:
        httpx_mock.add_response(url=f"{API}?q=garlic", **fault)

    response = client.get(SEARCH, {"q": "garlic"})

    assert response.status_code == 503
    assert "Social search is unavailable right now" in response.content.decode()


def test_a_failure_is_not_cached(client, httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=f"{API}?q=garlic")
    httpx_mock.add_response(url=f"{API}?q=garlic", json=[POST])

    client.get(SEARCH, {"q": "garlic"})
    response = client.get(SEARCH, {"q": "garlic"})

    assert "Garlic planted today" in response.content.decode()


def test_no_query_asks_nothing(client, httpx_mock):
    response = client.get(SEARCH, {"q": "  "})

    assert response.status_code == 200
    assert "No posts found" not in response.content.decode()
    assert not httpx_mock.get_requests()


def test_no_results_says_so(client, httpx_mock):
    httpx_mock.add_response(url=f"{API}?q=%3Cb%3Ezzz", json=[])

    page = client.get(SEARCH, {"q": "<b>zzz"}).content.decode()

    assert "No posts found for <strong>&lt;b&gt;zzz</strong>" in page
