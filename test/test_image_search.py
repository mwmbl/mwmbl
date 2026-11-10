"""Tests for the Wikimedia Commons client and the deterministic rules in front of image search (no network)."""

from datetime import datetime, timezone

import httpx
import pytest
from django.conf import settings
from django.core.cache import cache

from mwmbl.imagesearch import commons, rules, verdict_cache
from mwmbl.imagesearch.types import ImageSourceUnavailable, SourceImage, Verdict

THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{}/320px-{}"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def make_page(name: str, index: int, categories: str = "Mammals|Zoos", description: str = "A red panda", **overrides):
    info = {
        "thumburl": THUMB.format(name, name),
        "thumbwidth": 320,
        "thumbheight": 213,
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}",
        "mime": "image/jpeg",
        "sha1": f"sha1-of-{name}",
        "timestamp": "2020-01-01T00:00:00Z",
        "extmetadata": {
            "Categories": {"value": categories},
            "ImageDescription": {"value": description},
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
            "Artist": {"value": '<a href="//example.org">A. Photographer</a>'},
        },
    }
    info.update(overrides)
    return {"title": f"File:{name}", "index": index, "imageinfo": [info]}


def make_image(title: str = "Red panda.jpg", rank: int = 1, **overrides) -> SourceImage:
    fields = dict(
        title=title,
        page_url="https://commons.wikimedia.org/wiki/File:x",
        thumbnail_url=THUMB.format(title, title),
        thumbnail_width=320,
        thumbnail_height=213,
        description="A red panda in a zoo",
        categories=("Mammals", "Zoos"),
        license_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0",
        attribution="A. Photographer",
        sha1=f"sha1-of-{title}",
        uploaded_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        rank=rank,
    )
    fields.update(overrides)
    return SourceImage(**fields)


# Deterministic rules


@pytest.mark.parametrize(
    "text",
    [
        "Nude woman",
        "sexual content",
        "Topless beach",
        "porn",
        "Naked mole rat",
        "OnlyFans",
        "cam girl",
        "busty",
        "milf",
    ],
)
def test_rules_deny_sexual_terms(text):
    assert rules.find_denied_term(text) is not None


@pytest.mark.parametrize("text", ["Essex county", "Sextant", "Red panda", "Middlesex", "Analysis of data"])
def test_rules_allow_innocent_lookalikes(text):
    assert rules.find_denied_term(text) is None


def test_rules_check_categories_as_well_as_title():
    image = make_image(categories=("Mammals", "Nude paintings"))
    assert rules.image_verdict(image) is Verdict.NSFW


def test_rules_refuse_a_sexual_query_and_pass_an_innocent_one():
    assert rules.query_verdict("nude beach") is Verdict.NSFW
    assert rules.query_verdict("red panda") is Verdict.SAFE


@pytest.mark.parametrize("text", ["n\u200bude", "nüde", "ｎｕｄｅ", "n\u00adaked", "NUDE"])
def test_rules_see_through_accents_width_and_invisible_characters(text):
    assert rules.find_denied_term(text) is not None


@pytest.mark.parametrize("text", ["topless_beach", "Woman_nude.jpg", "Naked_mole_rat", "2naked", "nude2024.jpg"])
def test_rules_see_through_underscores_and_digits_in_file_names(text):
    assert rules.find_denied_term(text) is not None


# Commons client


def test_parse_page_extracts_everything_the_checks_read():
    image = commons.parse_page(make_page("Red panda.jpg", 3))
    assert image.title == "Red panda.jpg"
    assert image.categories == ("Mammals", "Zoos")
    assert image.attribution == "A. Photographer"
    assert image.rank == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"mime": "image/svg+xml"},
        {"thumburl": "https://evil.example.com/a.jpg"},
        {"thumburl": "http://upload.wikimedia.org/a.jpg"},
        {"thumburl": ""},
    ],
)
def test_parse_page_drops_unusable_thumbnails(overrides):
    assert commons.parse_page(make_page("a.jpg", 1, **overrides)) is None


def test_parse_page_drops_images_without_categories():
    assert commons.parse_page(make_page("a.jpg", 1, categories="")) is None


def test_parse_page_drops_pages_without_imageinfo():
    assert commons.parse_page({"title": "File:a.jpg", "index": 1}) is None


def test_parse_page_drops_a_description_page_that_is_not_on_commons():
    page = make_page("a.jpg", 1, descriptionurl="https://evil.example.com/wiki/File:a.jpg")
    assert commons.parse_page(page) is None


def test_encoded_markup_in_metadata_is_not_revived():
    page = make_page("a.jpg", 1)
    page["imageinfo"][0]["extmetadata"]["Artist"] = {"value": "Bob &lt;img src=x onerror=alert(1)&gt; &amp; Co"}
    image = commons.parse_page(page)
    assert "<" not in image.attribution
    assert image.attribution == "Bob & Co"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://example.org/licence", "//example.org/licence"])
def test_licence_url_must_be_https(url):
    page = make_page("a.jpg", 1)
    page["imageinfo"][0]["extmetadata"]["LicenseUrl"] = {"value": url}
    assert commons.parse_page(page).license_url == ""


def test_search_commons_sends_user_agent_and_orders_by_rank(httpx_mock):
    pages = [make_page("b.jpg", 2), make_page("a.jpg", 1)]
    httpx_mock.add_response(json={"query": {"pages": pages}})
    with commons.new_client() as client:
        images = commons.search_commons(client, "red panda", 5)
    assert [image.title for image in images] == ["a.jpg", "b.jpg"]
    request = httpx_mock.get_requests()[0]
    assert request.headers["User-Agent"] == settings.IMAGE_SEARCH_USER_AGENT
    assert "filetype:bitmap" in request.url.params["gsrsearch"]


def test_search_commons_falls_back_to_plain_search_when_rejected(httpx_mock):
    httpx_mock.add_response(status_code=400)
    httpx_mock.add_response(json={"query": {"pages": [make_page("a.jpg", 1)]}})
    with commons.new_client() as client:
        images = commons.search_commons(client, "red panda", 5)
    assert len(images) == 1
    assert httpx_mock.get_requests()[1].url.params["gsrsearch"] == "red panda"


def test_search_commons_falls_back_to_plain_search_when_the_api_reports_an_error(httpx_mock):
    httpx_mock.add_response(json={"error": {"code": "badvalue", "info": "bad profile"}})
    httpx_mock.add_response(json={"query": {"pages": [make_page("a.jpg", 1)]}})
    with commons.new_client() as client:
        assert len(commons.search_commons(client, "red panda", 5)) == 1


def test_a_failing_fallback_is_unavailable_not_an_unhandled_error(httpx_mock):
    httpx_mock.add_response(status_code=400)
    httpx_mock.add_response(status_code=502)
    with commons.new_client() as client:
        with pytest.raises(ImageSourceUnavailable):
            commons.search_commons(client, "red panda", 5)


def test_a_body_that_is_not_json_is_unavailable(httpx_mock):
    httpx_mock.add_response(content=b"<html>oops</html>")
    with commons.new_client() as client:
        with pytest.raises(ImageSourceUnavailable):
            commons.search_commons(client, "red panda", 5)


def test_search_syntax_in_the_query_is_not_passed_to_commons(httpx_mock):
    httpx_mock.add_response(json={"query": {"pages": []}})
    with commons.new_client() as client:
        commons.search_commons(client, 'incategory:"Cats" -dogs insource:/x/ red*', 5)
    expression = httpx_mock.get_requests()[0].url.params["gsrsearch"]
    assert expression == "filetype:bitmap -fileres:0 incategory Cats dogs insource /x/ red"


def test_a_query_that_is_only_syntax_searches_nothing(httpx_mock):
    with commons.new_client() as client:
        assert commons.search_commons(client, ':"-', 5) == []
    assert httpx_mock.get_requests() == []


def test_search_commons_with_no_matches_is_empty(httpx_mock):
    httpx_mock.add_response(json={"batchcomplete": True})
    with commons.new_client() as client:
        assert commons.search_commons(client, "zzzz", 5) == []


def test_rate_limit_is_respected_and_pauses_later_requests(httpx_mock):
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "30"})
    with commons.new_client() as client:
        with pytest.raises(ImageSourceUnavailable):
            commons.search_commons(client, "red panda", 5)
        # The second call must not reach Wikimedia at all.
        with pytest.raises(ImageSourceUnavailable):
            commons.search_commons(client, "red panda", 5)
    assert len(httpx_mock.get_requests()) == 1


def test_network_failure_is_unavailable_not_empty(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectTimeout("slow"))
    with commons.new_client() as client:
        with pytest.raises(ImageSourceUnavailable):
            commons.search_commons(client, "red panda", 5)


# Cache keys


def test_cache_keys_differ_by_kind_fingerprint_and_content():
    key = verdict_cache.cache_key("kind", "fingerprint", "content")
    others = [
        verdict_cache.cache_key("other", "fingerprint", "content"),
        verdict_cache.cache_key("kind", "other", "content"),
        verdict_cache.cache_key("kind", "fingerprint", "other"),
    ]
    assert key.startswith(verdict_cache.KEY_PREFIX)
    assert key not in others
