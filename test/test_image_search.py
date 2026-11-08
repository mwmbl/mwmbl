"""Tests for image search: the Commons client, each safety layer, and the fail-closed pipeline.

The models are replaced by fakes, so these run without them; the pipeline's job is the
combination: an image is shown only if every layer says SAFE, and anything missing or failing
is excluded. The tests that run the real models are skipped when they have not been built.
"""

import io
from pathlib import Path

import httpx
import numpy as np
import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from PIL import Image

from mwmbl.imagesearch import commons, pipeline, rules
from mwmbl.imagesearch.pixel_classifier import (
    CLASS_NAMES,
    PixelSafetyClassifier,
    fetch_thumbnail,
    verdict_for_scores,
)
from mwmbl.imagesearch.text_classifier import verdict_for_probability
from mwmbl.imagesearch.types import ImageSourceUnavailable, SourceImage, Verdict

MODELS_BUILT = (Path(settings.IMAGE_SEARCH_MODEL_DIR) / "nudenet" / "320n.onnx").exists()
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
        rank=rank,
    )
    fields.update(overrides)
    return SourceImage(**fields)


class FakeText:
    """Calls a text unsafe if it contains the word 'risky', uncertain if it contains 'maybe'."""

    def classify(self, texts):
        def verdict(text):
            if "risky" in text:
                return Verdict.NSFW
            return Verdict.UNCERTAIN if "maybe" in text else Verdict.SAFE

        return [verdict(text) for text in texts]


class FakePixels:
    def __init__(self, verdict=Verdict.SAFE):
        self.verdict = verdict
        self.seen = 0

    def classify_bytes(self, data):
        self.seen += 1
        return self.verdict


@pytest.fixture
def fake_models(monkeypatch):
    pixels = FakePixels()
    monkeypatch.setattr(pipeline, "text_classifier", lambda: FakeText())
    monkeypatch.setattr(pipeline, "pixel_classifier", lambda: pixels)
    monkeypatch.setattr(pipeline, "fetch_thumbnail", lambda client, url: b"bytes")
    return pixels


def stub_commons(monkeypatch, images):
    calls = []

    def search(client, query, limit):
        calls.append((query, limit))
        return images

    monkeypatch.setattr(pipeline, "search_commons", search)
    return calls


# Deterministic rules


@pytest.mark.parametrize("text", ["Nude woman", "sexual content", "Topless beach", "porn", "Naked mole rat"])
def test_rules_deny_sexual_terms(text):
    assert rules.find_denied_term(text) is not None


@pytest.mark.parametrize("text", ["Essex county", "Sextant", "Red panda", "Middlesex", "Analysis of data"])
def test_rules_allow_innocent_lookalikes(text):
    assert rules.find_denied_term(text) is None


def test_rules_check_categories_as_well_as_title():
    image = make_image(categories=("Mammals", "Nude paintings"))
    assert rules.image_verdict(image) is Verdict.NSFW


# Text classifier thresholds


def test_text_thresholds_split_safe_uncertain_nsfw():
    assert verdict_for_probability(0.001) is Verdict.SAFE
    assert verdict_for_probability(settings.IMAGE_SEARCH_TEXT_UNCERTAIN_THRESHOLD) is Verdict.UNCERTAIN
    assert verdict_for_probability(settings.IMAGE_SEARCH_TEXT_NSFW_THRESHOLD) is Verdict.NSFW


# Pixel classifier


def scores_with(**by_class):
    scores = np.zeros(len(CLASS_NAMES))
    for name, score in by_class.items():
        scores[CLASS_NAMES.index(name)] = score
    return scores


def test_pixel_exposed_body_part_is_nsfw():
    assert verdict_for_scores(scores_with(FEMALE_BREAST_EXPOSED=0.9)) is Verdict.NSFW


def test_pixel_covered_body_part_is_uncertain_only_when_confident():
    assert verdict_for_scores(scores_with(BUTTOCKS_COVERED=0.7)) is Verdict.UNCERTAIN
    assert verdict_for_scores(scores_with(BUTTOCKS_COVERED=0.3)) is Verdict.SAFE


def test_pixel_faces_and_feet_are_safe():
    assert verdict_for_scores(scores_with(FACE_FEMALE=0.99, FEET_EXPOSED=0.99)) is Verdict.SAFE


def test_fetch_thumbnail_refuses_other_hosts(httpx_mock):
    with commons.new_client() as client:
        assert fetch_thumbnail(client, "https://evil.example.com/a.jpg") is None


def test_fetch_thumbnail_refuses_oversized(httpx_mock, settings):
    settings.IMAGE_SEARCH_THUMBNAIL_MAX_BYTES = 10
    httpx_mock.add_response(content=b"x" * 100)
    with commons.new_client() as client:
        assert fetch_thumbnail(client, THUMB.format("a.jpg", "a.jpg")) is None


def test_fetch_thumbnail_returns_none_on_error_status(httpx_mock):
    httpx_mock.add_response(status_code=404)
    with commons.new_client() as client:
        assert fetch_thumbnail(client, THUMB.format("a.jpg", "a.jpg")) is None


@pytest.mark.skipif(not MODELS_BUILT, reason="image safety models not built")
def test_undecodable_thumbnail_is_uncertain():
    classifier = PixelSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)
    assert classifier.classify_bytes(b"not an image") is Verdict.UNCERTAIN


@pytest.mark.skipif(not MODELS_BUILT, reason="image safety models not built")
def test_blank_image_is_safe():
    classifier = PixelSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)
    buffer = io.BytesIO()
    Image.new("RGB", (320, 200), (30, 120, 200)).save(buffer, format="PNG")
    assert classifier.classify_bytes(buffer.getvalue()) is Verdict.SAFE


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


# Pipeline


def test_safe_image_is_approved(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image()])
    (approved,) = pipeline.search_images("red panda", 10)
    assert approved.title == "Red panda.jpg"
    assert approved.source == "wikimedia-commons"
    assert fake_models.seen == 1


def test_each_layer_can_veto(monkeypatch, fake_models):
    images = [
        make_image("ok.jpg", 1),
        make_image("rules.jpg", 2, categories=("Nude art",)),
        make_image("text-nsfw.jpg", 3, description="risky"),
        make_image("text-uncertain.jpg", 4, description="maybe"),
    ]
    stub_commons(monkeypatch, images)
    titles = [a.title for a in pipeline.search_images("art", 10)]
    assert titles == ["ok.jpg"]
    # Images failing the text layers never reach the pixel layer, so no thumbnail is fetched for them.
    assert fake_models.seen == 1


@pytest.mark.parametrize("verdict", [Verdict.NSFW, Verdict.UNCERTAIN])
def test_pixel_layer_vetoes(monkeypatch, fake_models, verdict):
    fake_models.verdict = verdict
    stub_commons(monkeypatch, [make_image()])
    assert pipeline.search_images("red panda", 10) == []


def test_failed_thumbnail_fetch_excludes_the_image(monkeypatch, fake_models):
    monkeypatch.setattr(pipeline, "fetch_thumbnail", lambda client, url: None)
    stub_commons(monkeypatch, [make_image()])
    assert pipeline.search_images("red panda", 10) == []
    assert fake_models.seen == 0


def test_unsafe_query_never_reaches_commons(monkeypatch, fake_models):
    calls = stub_commons(monkeypatch, [make_image()])
    assert pipeline.search_images("nude photos", 10) == []
    assert pipeline.search_images("risky business", 10) == []
    assert calls == []


def test_source_outage_propagates_instead_of_returning_nothing(monkeypatch, fake_models):
    def search(client, query, limit):
        raise ImageSourceUnavailable("down")

    monkeypatch.setattr(pipeline, "search_commons", search)
    with pytest.raises(ImageSourceUnavailable):
        pipeline.search_images("red panda", 10)


def test_limit_truncates_and_results_are_cached(monkeypatch, fake_models):
    calls = stub_commons(monkeypatch, [make_image(f"p{i}.jpg", i) for i in range(1, 6)])
    assert len(pipeline.search_images("red panda", 3)) == 3
    assert len(pipeline.search_images("  Red   Panda ", 5)) == 5
    assert len(calls) == 1


def test_only_approved_images_are_cached(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("ok.jpg", 1), make_image("bad.jpg", 2, categories=("Nude art",))])
    pipeline.search_images("art", 10)
    cached = cache.get(pipeline.cache_key("search", "art"))
    assert [a.title for a in cached] == ["ok.jpg"]


# Top image


def test_strong_match_needs_every_term_in_the_title_and_a_high_rank():
    assert pipeline.is_strong_match("red panda", make_image("Red Panda.JPG", 1))
    assert pipeline.is_strong_match("red pandas", make_image("Red Panda.JPG", 1))
    assert not pipeline.is_strong_match("red panda", make_image("Red Fox.jpg", 1))
    assert not pipeline.is_strong_match("red panda", make_image("Red Panda.JPG", 9))
    assert not pipeline.is_strong_match("a b c d e f", make_image("a b c d e f.jpg", 1))
    assert not pipeline.is_strong_match("the", make_image("the.jpg", 1))


def test_top_image_is_the_first_strong_safe_match(monkeypatch, fake_models):
    stub_commons(
        monkeypatch,
        [make_image("Red Fox.jpg", 1), make_image("Red Panda 1.jpg", 2), make_image("Red Panda 2.jpg", 3)],
    )
    image = pipeline.top_image("red panda")
    assert image.title == "Red Panda 1.jpg"
    assert fake_models.seen == 2


def test_top_image_is_none_without_a_strong_match(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("Red Fox.jpg", 1)])
    assert pipeline.top_image("red panda") is None
    assert fake_models.seen == 0


def test_top_image_is_none_for_a_sexual_query(monkeypatch, fake_models):
    calls = stub_commons(monkeypatch, [make_image("Nude.jpg", 1)])
    assert pipeline.top_image("nude") is None
    assert calls == []


# Endpoints


@pytest.fixture
def client():
    return Client()


def test_endpoint_returns_approved_images(client, monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image()])
    response = client.get("/api/v2/image-search/", {"q": "red panda"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "red panda"
    assert body["results"][0]["thumbnail_url"].startswith("https://upload.wikimedia.org/")
    assert body["results"][0]["source"] == "wikimedia-commons"


def test_endpoint_is_503_when_the_source_is_unavailable(client, monkeypatch, fake_models):
    def search(client, query, limit):
        raise ImageSourceUnavailable("down")

    monkeypatch.setattr(pipeline, "search_commons", search)
    assert client.get("/api/v2/image-search/", {"q": "red panda"}).status_code == 503


def test_endpoint_rejects_out_of_range_limit(client):
    assert client.get("/api/v2/image-search/", {"q": "red panda", "limit": 0}).status_code == 422
    assert client.get("/api/v2/image-search/", {"q": "red panda", "limit": 1000}).status_code == 422


def test_top_endpoint_returns_null_without_a_match(client, monkeypatch, fake_models):
    stub_commons(monkeypatch, [])
    response = client.get("/api/v2/image-search/top/", {"q": "red panda"})
    assert response.status_code == 200
    assert response.json() == {"query": "red panda", "image": None}
