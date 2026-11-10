"""Tests for image search: the Commons client, each safety layer, and the fail-closed pipeline.

The models are replaced by fakes, so these run without them; the pipeline's job is the
combination: an image is shown only if every layer says SAFE, and anything missing or failing
is excluded. The tests that run the real models are skipped when they have not been built.
"""

import io
from datetime import datetime, timedelta, timezone
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
from mwmbl.imagesearch.text_classifier import (
    MAX_CHUNKS,
    WORDS_PER_CHUNK,
    TextSafetyClassifier,
    split_into_chunks,
    verdict_for_probability,
)
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
    revisions = [{"timestamp": "2021-01-01T00:00:00Z"}]
    return {"title": f"File:{name}", "index": index, "imageinfo": [info], "revisions": revisions}


def make_image(title: str = "Red panda.jpg", rank: int = 1, **overrides) -> SourceImage:
    fields = dict(
        title=title,
        caption="",
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
        revised_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
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
    fingerprint = "fake-pixel-model"

    def __init__(self, verdict=Verdict.SAFE):
        self.verdict = verdict
        self.seen = 0

    def classify_bytes(self, data):
        self.seen += 1
        return self.verdict


class FakeZeroShot(FakePixels):
    fingerprint = "fake-zero-shot-model"


@pytest.fixture
def fake_zero_shot(monkeypatch, settings, fake_models):
    """The zero-shot layer switched on, with a fake model. The NudeNet fake is `fake_models`."""
    settings.IMAGE_SEARCH_ZERO_SHOT = "enforce"
    zero_shot = FakeZeroShot()
    monkeypatch.setattr(pipeline, "zero_shot_classifier", lambda: zero_shot)
    return zero_shot


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
        "Naturism in France",
        "nudism",
        "FKK-Strand",
        "Freikörperkultur",
        "skinny dipping",
        "Clothing-optional beach",
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


@pytest.mark.parametrize("text", ["n\u200bude", "nüde", "ｎｕｄｅ", "n\u00adaked", "NUDE"])
def test_rules_see_through_accents_width_and_invisible_characters(text):
    assert rules.find_denied_term(text) is not None


@pytest.mark.parametrize("text", ["topless_beach", "Woman_nude.jpg", "Naked_mole_rat", "2naked", "nude2024.jpg"])
def test_rules_see_through_underscores_and_digits_in_file_names(text):
    assert rules.find_denied_term(text) is not None


# Text classifier chunking


def test_short_text_is_one_chunk():
    assert split_into_chunks("a red panda") == ["a red panda"]


def test_long_text_is_split_into_chunks_that_cover_every_word():
    text = " ".join(f"w{i}" for i in range(WORDS_PER_CHUNK * 2 + 5))
    chunks = split_into_chunks(text)
    assert len(chunks) == 3
    assert " ".join(chunks) == text


def test_text_too_long_to_check_or_empty_has_no_chunks():
    assert split_into_chunks("word " * (WORDS_PER_CHUNK * MAX_CHUNKS + 1)) is None
    assert split_into_chunks("   ") is None


@pytest.mark.skipif(not MODELS_BUILT, reason="image safety models not built")
class TestRealTextClassifier:
    @pytest.fixture(scope="class")
    def classifier(self):
        return TextSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)

    def test_plain_text_is_safe(self, classifier):
        assert classifier.classify(["Red panda in Munich Zoo"]) == [Verdict.SAFE]

    def test_unsafe_text_hidden_after_a_long_safe_prefix_is_still_caught(self, classifier):
        prefix = " ".join(["a quiet photograph of a lake at dawn"] * 40)
        (verdict,) = classifier.classify([prefix + " hardcore porn video"])
        assert verdict is not Verdict.SAFE

    def test_text_too_long_to_check_and_empty_text_are_uncertain(self, classifier):
        too_long = "lake " * (WORDS_PER_CHUNK * MAX_CHUNKS + 1)
        assert classifier.classify([too_long, ""]) == [Verdict.UNCERTAIN, Verdict.UNCERTAIN]

    @pytest.mark.parametrize("query", ["gorilla", "monkey", "ape", "cat", "lion", "mona lisa", "red panda"])
    def test_ordinary_queries_are_not_refused(self, classifier, query):
        # Bare, the model calls some of these NSFW; the gate judges them framed as a caption.
        (verdict,) = classifier.classify([f"a photo of {query}"])
        assert verdict is not Verdict.NSFW

    def test_each_text_gets_its_own_verdict(self, classifier):
        verdicts = classifier.classify(["Eiffel Tower at night", "hardcore porn video", "Red panda in Munich Zoo"])
        assert verdicts == [Verdict.SAFE, Verdict.NSFW, Verdict.SAFE]


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


def test_fetch_thumbnail_returns_the_bytes_of_a_small_thumbnail(httpx_mock):
    httpx_mock.add_response(content=b"thumbnail bytes")
    with commons.new_client() as client:
        assert fetch_thumbnail(client, THUMB.format("a.jpg", "a.jpg")) == b"thumbnail bytes"


def test_fetch_thumbnail_returns_none_on_error_status(httpx_mock):
    httpx_mock.add_response(status_code=404)
    with commons.new_client() as client:
        assert fetch_thumbnail(client, THUMB.format("a.jpg", "a.jpg")) is None


def test_fetch_thumbnail_raises_when_wikimedia_is_unavailable(httpx_mock):
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "5"})
    with commons.new_client() as client:
        with pytest.raises(ImageSourceUnavailable):
            fetch_thumbnail(client, THUMB.format("a.jpg", "a.jpg"))


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
    assert image.caption == ""
    assert image.revised_at == datetime(2021, 1, 1, tzinfo=timezone.utc)


def test_parse_page_reads_the_english_caption_as_plain_text():
    page = make_page("a.jpg", 1) | {"entityterms": {"label": ["A red panda &lt;b&gt;asleep&lt;/b&gt;"]}}
    image = commons.parse_page(page)
    assert image.caption == "A red panda asleep"
    assert "A red panda asleep" in image.text


def test_parse_page_drops_pages_without_a_revision_time():
    page = make_page("a.jpg", 1)
    del page["revisions"]
    assert commons.parse_page(page) is None


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
    assert request.url.params["prop"] == "imageinfo|entityterms|revisions"
    assert request.url.params["iiextmetadatalanguage"] == "en"


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


# Pipeline


def test_safe_image_is_approved(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image()])
    (approved,) = pipeline.search_images("red panda", 10)
    assert approved.title == "Red panda.jpg"
    assert approved.source == "wikimedia-commons"
    assert fake_models.seen == 1


def test_each_layer_can_veto(monkeypatch, fake_models):
    images = [make_image("ok.jpg", 1), make_image("rules.jpg", 2, categories=("Nude art",))]
    stub_commons(monkeypatch, images)
    titles = [a.title for a in pipeline.search_images("art", 10)]
    assert titles == ["ok.jpg"]
    # An image failing the rules never reaches the pixel layer, so no thumbnail is fetched for it.
    assert fake_models.seen == 1


def test_image_text_is_judged_by_the_rules_not_the_text_classifier(monkeypatch, fake_models):
    seen = []

    class RecordingText(FakeText):
        def classify(self, texts):
            seen.extend(texts)
            return super().classify(texts)

    monkeypatch.setattr(pipeline, "text_classifier", lambda: RecordingText())
    # The classifier would call this text NSFW; only the deny rules and the pixel layer judge it now.
    stub_commons(monkeypatch, [make_image("a.jpg", 1, description="risky")])
    assert [a.title for a in pipeline.search_images("red panda", 10)] == ["a.jpg"]
    assert seen == ["a photo of red panda"]


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


def test_wikimedia_unavailable_while_checking_thumbnails_is_not_a_cached_empty_result(monkeypatch, fake_models):
    def unavailable(client, url):
        raise ImageSourceUnavailable("rate limited")

    monkeypatch.setattr(pipeline, "fetch_thumbnail", unavailable)
    stub_commons(monkeypatch, [make_image()])
    with pytest.raises(ImageSourceUnavailable):
        pipeline.search_images("red panda", 10)
    assert cache.get(pipeline.cache_key("search", "red panda")) is None

    with pytest.raises(ImageSourceUnavailable):
        pipeline.top_image("red panda")
    assert cache.get(pipeline.cache_key("top", "red panda")) is None


def test_unsafe_query_never_reaches_commons(monkeypatch, fake_models):
    calls = stub_commons(monkeypatch, [make_image()])
    assert pipeline.search_images("nude photos", 10) == []
    assert pipeline.search_images("risky business", 10) == []
    assert calls == []


def test_the_query_is_judged_as_a_caption_not_a_bare_word(monkeypatch, fake_models):
    seen = []

    class RecordingText(FakeText):
        def classify(self, texts):
            seen.extend(texts)
            return super().classify(texts)

    monkeypatch.setattr(pipeline, "text_classifier", lambda: RecordingText())
    stub_commons(monkeypatch, [make_image()])
    pipeline.search_images("gorilla", 10)
    assert seen[0] == "a photo of gorilla"


def test_an_uncertain_query_still_searches_but_images_must_pass_every_layer(monkeypatch, fake_models):
    calls = stub_commons(monkeypatch, [make_image()])
    assert len(pipeline.search_images("maybe a cat", 10)) == 1
    assert len(calls) == 1


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


# Verdict cache


def test_a_replaced_file_is_checked_afresh_even_at_the_same_thumbnail_url(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    assert len(pipeline.search_images("first", 10)) == 1
    assert fake_models.seen == 1

    fake_models.verdict = Verdict.NSFW
    stub_commons(monkeypatch, [make_image("a.jpg", 1, sha1="the-replacement")])
    assert pipeline.search_images("second", 10) == []
    assert fake_models.seen == 2


def test_a_pixel_verdict_is_reused_for_the_same_file(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    pipeline.search_images("first", 10)
    fake_models.verdict = Verdict.NSFW
    # Same file contents: the earlier verdict stands without fetching or classifying it again.
    assert len(pipeline.search_images("second", 10)) == 1
    assert fake_models.seen == 1


@pytest.mark.parametrize("verdict", [Verdict.SAFE, Verdict.NSFW])
def test_a_pixel_verdict_from_the_model_is_remembered(monkeypatch, fake_models, verdict):
    fake_models.verdict = verdict
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    pipeline.search_images("first", 10)
    pipeline.search_images("second", 10)
    assert fake_models.seen == 1


def test_an_uncertain_pixel_verdict_is_not_remembered(monkeypatch, fake_models):
    fake_models.verdict = Verdict.UNCERTAIN
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    pipeline.search_images("first", 10)
    fake_models.verdict = Verdict.SAFE
    assert len(pipeline.search_images("second", 10)) == 1
    assert fake_models.seen == 2


def test_a_failed_thumbnail_fetch_is_not_remembered(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    monkeypatch.setattr(pipeline, "fetch_thumbnail", lambda client, url: None)
    assert pipeline.search_images("first", 10) == []
    monkeypatch.setattr(pipeline, "fetch_thumbnail", lambda client, url: b"bytes")
    assert len(pipeline.search_images("second", 10)) == 1


def test_verdicts_do_not_survive_a_change_of_model_or_threshold(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    pipeline.search_images("first", 10)
    FakePixels.fingerprint = "a-retrained-pixel-model"
    try:
        pipeline.search_images("second", 10)
    finally:
        del FakePixels.fingerprint
        FakePixels.fingerprint = "fake-pixel-model"
    assert fake_models.seen == 2


def test_a_just_uploaded_file_is_checked_every_time(monkeypatch, fake_models):
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    stub_commons(monkeypatch, [make_image("new.jpg", 1, uploaded_at=fresh)])
    pipeline.search_images("first", 10)
    pipeline.search_images("second", 10)
    assert fake_models.seen == 2


def test_a_batch_mixing_remembered_and_new_files_keeps_each_verdict_with_its_image(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("remembered-unsafe.jpg", 1)])
    fake_models.verdict = Verdict.NSFW
    pipeline.search_images("first", 10)
    stub_commons(monkeypatch, [make_image("remembered-safe.jpg", 1)])
    fake_models.verdict = Verdict.SAFE
    pipeline.search_images("second", 10)

    fake_models.verdict = Verdict.SAFE
    stub_commons(
        monkeypatch,
        [make_image("remembered-unsafe.jpg", 1), make_image("new.jpg", 2), make_image("remembered-safe.jpg", 3)],
    )
    titles = [image.title for image in pipeline.search_images("third", 10)]
    assert titles == ["new.jpg", "remembered-safe.jpg"]
    assert fake_models.seen == 3


def test_images_that_are_the_same_file_are_fetched_and_judged_once(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("a.jpg", 1, sha1="same"), make_image("b.jpg", 2, sha1="same")])
    assert len(pipeline.search_images("first", 10)) == 2
    assert fake_models.seen == 1


def test_the_same_file_at_another_thumbnail_width_is_judged_separately(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("a.jpg", 1, sha1="same", thumbnail_width=320)])
    pipeline.search_images("first", 10)
    stub_commons(monkeypatch, [make_image("a.jpg", 1, sha1="same", thumbnail_width=200)])
    pipeline.search_images("second", 10)
    assert fake_models.seen == 2


def test_nothing_is_remembered_when_a_thumbnail_fetch_fails_part_way(monkeypatch, fake_models):
    def fetch(client, url):
        if "b.jpg" in url:
            raise ImageSourceUnavailable("rate limited")
        return b"bytes"

    monkeypatch.setattr(pipeline, "fetch_thumbnail", fetch)
    stub_commons(monkeypatch, [make_image("a.jpg", 1), make_image("b.jpg", 2)])
    with pytest.raises(ImageSourceUnavailable):
        pipeline.search_images("first", 10)
    assert not [key for key in cache._cache if "verdict:v1:pixel" in key]


def test_a_page_without_a_hash_or_upload_time_is_dropped():
    page = make_page("a.jpg", 1)
    assert commons.parse_page(page) is not None
    for field in ("sha1", "timestamp"):
        without = make_page("a.jpg", 1)
        del without["imageinfo"][0][field]
        assert commons.parse_page(without) is None


def test_only_verdicts_are_stored(monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("a.jpg", 1, description="distinctive description")])
    pipeline.search_images("first", 10)
    stored = [value for key, value in cache._cache.items() if "verdict" in key]
    assert stored
    assert all("distinctive" not in repr(value) for value in stored)


# Zero-shot layer


def test_the_zero_shot_layer_is_off_unless_enabled(monkeypatch, fake_models):
    def not_loaded():
        raise AssertionError("the zero-shot model must not be loaded while the layer is off")

    monkeypatch.setattr(pipeline, "zero_shot_classifier", not_loaded)
    stub_commons(monkeypatch, [make_image()])
    assert len(pipeline.search_images("red panda", 10)) == 1


def test_an_unknown_zero_shot_mode_fails_loudly(monkeypatch, settings, fake_models):
    settings.IMAGE_SEARCH_ZERO_SHOT = "shadow-ish"
    stub_commons(monkeypatch, [make_image()])
    with pytest.raises(ValueError, match="IMAGE_SEARCH_ZERO_SHOT"):
        pipeline.search_images("red panda", 10)


def test_the_zero_shot_layer_can_veto_what_nudenet_passed(monkeypatch, fake_models, fake_zero_shot):
    fake_zero_shot.verdict = Verdict.NSFW
    stub_commons(monkeypatch, [make_image()])
    assert pipeline.search_images("art", 10) == []
    assert fake_models.seen == 1 and fake_zero_shot.seen == 1


@pytest.mark.parametrize("zero_shot_verdict", [Verdict.NSFW, Verdict.UNCERTAIN])
def test_an_image_needs_every_pixel_layer_to_say_safe(monkeypatch, fake_models, fake_zero_shot, zero_shot_verdict):
    stub_commons(monkeypatch, [make_image("both.jpg", 1)])
    assert [a.title for a in pipeline.search_images("first", 10)] == ["both.jpg"]

    fake_zero_shot.verdict = zero_shot_verdict
    stub_commons(monkeypatch, [make_image("other.jpg", 1, sha1="a-different-file")])
    assert pipeline.search_images("second", 10) == []
    fake_models.verdict = Verdict.NSFW
    fake_zero_shot.verdict = Verdict.SAFE
    stub_commons(monkeypatch, [make_image("third.jpg", 1, sha1="a-third-file")])
    assert pipeline.search_images("third", 10) == []


def test_the_thumbnail_is_fetched_once_for_all_the_layers(monkeypatch, fake_models, fake_zero_shot):
    fetched = []
    monkeypatch.setattr(pipeline, "fetch_thumbnail", lambda client, url: fetched.append(url) or b"bytes")
    stub_commons(monkeypatch, [make_image("a.jpg", 1), make_image("b.jpg", 2)])
    assert len(pipeline.search_images("red panda", 10)) == 2
    assert len(fetched) == 2
    assert fake_models.seen == fake_zero_shot.seen == 2


def test_each_layers_verdicts_are_cached_on_their_own(monkeypatch, settings, fake_models, fake_zero_shot):
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    pipeline.search_images("first", 10)
    assert fake_models.seen == fake_zero_shot.seen == 1

    # Turning the layer off reuses NudeNet's cached verdict; turning it back on reuses the zero-shot one.
    settings.IMAGE_SEARCH_ZERO_SHOT = "off"
    pipeline.search_images("second", 10)
    settings.IMAGE_SEARCH_ZERO_SHOT = "enforce"
    pipeline.search_images("third", 10)
    assert fake_models.seen == fake_zero_shot.seen == 1


def test_changing_the_zero_shot_model_or_threshold_leaves_nudenets_cached_verdicts_valid(
    monkeypatch, fake_models, fake_zero_shot
):
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    pipeline.search_images("first", 10)
    FakeZeroShot.fingerprint = "a-retuned-zero-shot-model"
    try:
        pipeline.search_images("second", 10)
    finally:
        FakeZeroShot.fingerprint = "fake-zero-shot-model"
    assert fake_models.seen == 1
    assert fake_zero_shot.seen == 2


def test_a_zero_shot_verdict_that_excludes_an_image_is_remembered(monkeypatch, fake_models, fake_zero_shot):
    fake_zero_shot.verdict = Verdict.NSFW
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    pipeline.search_images("first", 10)
    fake_zero_shot.verdict = Verdict.SAFE
    # The earlier verdict stands for the same file; it is not judged again.
    assert pipeline.search_images("second", 10) == []
    assert fake_zero_shot.seen == 1


def test_an_uncertain_zero_shot_verdict_is_not_remembered(monkeypatch, fake_models, fake_zero_shot):
    fake_zero_shot.verdict = Verdict.UNCERTAIN
    stub_commons(monkeypatch, [make_image("a.jpg", 1)])
    pipeline.search_images("first", 10)
    fake_zero_shot.verdict = Verdict.SAFE
    assert len(pipeline.search_images("second", 10)) == 1
    assert fake_zero_shot.seen == 2
    assert fake_models.seen == 1  # NudeNet's SAFE was kept; only the zero-shot layer was asked again


def test_a_failed_thumbnail_fetch_excludes_the_image_from_every_layer(monkeypatch, fake_models, fake_zero_shot):
    monkeypatch.setattr(pipeline, "fetch_thumbnail", lambda client, url: None)
    stub_commons(monkeypatch, [make_image()])
    assert pipeline.search_images("red panda", 10) == []
    assert fake_models.seen == fake_zero_shot.seen == 0


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


def test_top_image_is_served_from_the_cache_without_asking_commons_again(monkeypatch, fake_models):
    calls = stub_commons(monkeypatch, [make_image("Red Panda 1.jpg", 1)])
    first = pipeline.top_image("red panda")
    assert pipeline.top_image("Red  Panda") == first
    assert len(calls) == 1


def test_a_cached_absence_of_a_top_image_is_remembered_too(monkeypatch, fake_models):
    calls = stub_commons(monkeypatch, [make_image("Red Fox.jpg", 1)])
    assert pipeline.top_image("red panda") is None
    assert pipeline.top_image("red panda") is None
    assert len(calls) == 1


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


def test_top_endpoint_returns_the_image(client, monkeypatch, fake_models):
    stub_commons(monkeypatch, [make_image("Red Panda.jpg", 1)])
    body = client.get("/api/v2/image-search/top/", {"q": "red panda"}).json()
    assert body["image"]["title"] == "Red Panda.jpg"


def test_top_endpoint_is_503_when_the_source_is_unavailable(client, monkeypatch, fake_models):
    def search(client, query, limit):
        raise ImageSourceUnavailable("down")

    monkeypatch.setattr(pipeline, "search_commons", search)
    assert client.get("/api/v2/image-search/top/", {"q": "red panda"}).status_code == 503


# The real zero-shot model, skipped when it has not been built.

ZERO_SHOT_BUILT = (Path(settings.IMAGE_SEARCH_MODEL_DIR) / "zero_shot" / "model.onnx").exists()


@pytest.mark.skipif(not ZERO_SHOT_BUILT, reason="the zero-shot model has not been built")
def test_real_zero_shot_model_runs_on_a_jpeg():
    from mwmbl.imagesearch.zero_shot_classifier import ZeroShotSafetyClassifier

    classifier = ZeroShotSafetyClassifier(settings.IMAGE_SEARCH_MODEL_DIR)
    buffer = io.BytesIO()
    Image.new("RGB", (320, 213), (120, 160, 90)).save(buffer, format="JPEG")
    # Blank images are not a fair test of the verdict: with no safe prompt for them, a solid black one scores
    # high. This only checks that the exported model, embeddings and preprocessing fit together.
    assert 0.0 <= classifier.nude_probability(Image.open(io.BytesIO(buffer.getvalue()))) <= 1.0
    assert classifier.classify_bytes(b"not an image") is Verdict.UNCERTAIN
