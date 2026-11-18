"""Searching Mwmbl Social's posts (mwmbl.social.search_posts), with its search API faked at the
HTTP boundary by pytest-httpx."""

from datetime import UTC, datetime

import pytest
from django.core.cache import cache

from mwmbl import social

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
