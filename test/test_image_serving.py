"""Commons files in search results, shown only as the store vouches for them (mwmbl.imagesearch.serving)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from asgiref.sync import async_to_sync
from django.test import Client
from test_image_search import make_image
from test_super_search import _stub_scoring, _stub_sources

import mwmbl.tinysearchengine.super_search as super_search
from mwmbl.imagesearch import judge, serving, store
from mwmbl.imagesearch.commons import commons_file_key
from mwmbl.imagesearch.types import ImageDocument
from mwmbl.models import CommonsImage
from mwmbl.search_setup import ranker
from mwmbl.tinysearchengine.indexer import Document

PANDA = make_image(
    "Red panda é.jpg",
    caption="A red panda",
    page_url="https://commons.wikimedia.org/wiki/File:Red_panda_%C3%A9.jpg",
)
# The same file as the crawler might have found it, on the mobile site, under text nobody judged.
PANDA_RESULT = Document("Crawled title", "https://commons.m.wikimedia.org/wiki/File:Red panda é.jpg", "Crawled.")
ZOO_RESULT = Document("Zoo", "https://commons.wikimedia.org/wiki/File:Zoo.jpg", "A zoo.")
WEB_RESULT = Document("Red pandas", "https://example.com/red-pandas", "All about red pandas.")
CATEGORY_RESULT = Document("Red pandas", "https://commons.wikimedia.org/wiki/Category:Red_pandas", "Files.")
NOT_COMMONS_RESULT = Document("Mirror", "https://example.com/wikimedia.org/wiki/File:Zoo.jpg", "A mirror.")
PANDA_IMAGE = {
    "thumbnail_url": PANDA.thumbnail_url,
    "thumbnail_width": 320,
    "thumbnail_height": 213,
    "license_name": "CC BY-SA 4.0",
    "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
    "attribution": "A. Photographer",
}


def stored(image=PANDA, checked_ago=timedelta(hours=1), **overrides) -> CommonsImage:
    row = CommonsImage(page_url=commons_file_key(image.page_url))
    checked_at = datetime.now(timezone.utc) - checked_ago
    store.approve(row, image, 0.25, judge.nsfw_fingerprint(), checked_at)
    for name, value in overrides.items():
        setattr(row, name, value)
    row.save()
    return row


@pytest.mark.django_db
@pytest.mark.parametrize("checked_ago", [timedelta(hours=1), timedelta(hours=47)])
def test_a_stored_file_is_served_with_its_image_and_the_text_that_was_judged(checked_ago):
    stored(checked_ago=checked_ago)

    served = serving.served([WEB_RESULT, PANDA_RESULT])

    assert served[0] is WEB_RESULT
    assert isinstance(served[1], ImageDocument)
    assert (served[1].url, served[1].title, served[1].extract) == (PANDA_RESULT.url, "A red panda", PANDA.description)
    assert served[1].image == PANDA_IMAGE


@pytest.mark.django_db
@pytest.mark.parametrize(
    "overrides",
    [
        {"revoked_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "revocation_reason": "nsfw"},
        {"checked_ago": timedelta(hours=49)},
    ],
    ids=["revoked", "stale"],
)
def test_a_revoked_or_stale_file_is_dropped_and_the_results_below_move_up(overrides):
    stored(**overrides)

    assert serving.served([PANDA_RESULT, WEB_RESULT]) == [WEB_RESULT]


@pytest.mark.django_db
def test_a_file_the_store_lacks_is_dropped_even_with_the_store_empty():
    """A Commons file page the crawler indexed was never judged, so it isn't shown either."""
    results = [ZOO_RESULT, WEB_RESULT, CATEGORY_RESULT, NOT_COMMONS_RESULT]

    assert serving.served(results) == [WEB_RESULT, CATEGORY_RESULT, NOT_COMMONS_RESULT]


@pytest.mark.django_db
def test_a_file_reached_by_two_urls_is_shown_once_where_it_ranks_highest():
    stored()
    desktop_result = Document("Red panda", PANDA.page_url, "A red panda.")

    served = serving.served([PANDA_RESULT, WEB_RESULT, desktop_result])

    assert [result.url for result in served] == [PANDA_RESULT.url, WEB_RESULT.url]


@pytest.mark.django_db
def test_every_commons_file_is_looked_up_in_one_query(django_assert_num_queries):
    stored()

    with django_assert_num_queries(1):
        served = serving.served([PANDA_RESULT, WEB_RESULT, ZOO_RESULT])

    assert [result.url for result in served] == [PANDA_RESULT.url, WEB_RESULT.url]


@pytest.mark.django_db
def test_results_with_no_commons_file_are_not_looked_up(django_assert_num_queries):
    results = [WEB_RESULT, CATEGORY_RESULT, NOT_COMMONS_RESULT]

    with django_assert_num_queries(0):
        served = serving.served(results)

    assert served is results


@pytest.fixture
def ranked(monkeypatch):
    def configure(results):
        # What the ranker inside ImageCapRanker and ServedImagesRanker returns: the router closed over the outer instance.
        monkeypatch.setattr(ranker.ranker.ranker, "search", lambda *args, **kwargs: list(results))

    return configure


@pytest.mark.django_db
def test_standard_search_gives_a_served_file_its_image_and_every_other_result_none(ranked):
    stored()
    ranked([WEB_RESULT, ZOO_RESULT, PANDA_RESULT])

    body = Client().get("/api/v2/search/?q=red+panda").json()

    assert [(hit["url"], hit["title"], hit["engine"], hit["score"]) for hit in body["results"]] == [
        (WEB_RESULT.url, WEB_RESULT.title, "mwmbl", 1.0),
        (PANDA_RESULT.url, "A red panda", "commons", 0.5),
    ]
    assert [hit["image"] for hit in body["results"]] == [None, PANDA_IMAGE]
    assert body["number_of_results"] == 2


@pytest.mark.django_db
def test_a_file_the_store_lacks_is_dropped_before_the_image_cap_counts_it(ranked):
    otter = make_image("Otter.jpg", caption="An otter", page_url="https://commons.wikimedia.org/wiki/File:Otter.jpg")
    fox = make_image("Fox.jpg", caption="A fox", page_url="https://commons.wikimedia.org/wiki/File:Fox.jpg")
    otter_result = Document("Crawled otter", otter.page_url, "Crawled.")
    fox_result = Document("Crawled fox", fox.page_url, "Crawled.")
    stored()
    stored(otter)
    stored(fox)
    # The unstored zoo file ranks first, so a cap counted before the store's check would leave only one image. The
    # fox is stored, and past the cap of 2.
    ranked([ZOO_RESULT, PANDA_RESULT, WEB_RESULT, otter_result, fox_result])

    body = Client().get("/api/v2/search/?q=red+panda").json()

    assert [hit["url"] for hit in body["results"]] == [PANDA_RESULT.url, WEB_RESULT.url, otter_result.url]
    assert all(isinstance(hit["image"], dict) for hit in (body["results"][0], body["results"][2]))


@pytest.mark.django_db
def test_the_v1_search_and_the_search_page_drop_a_file_the_store_lacks(ranked):
    ranked([ZOO_RESULT, WEB_RESULT])

    v1_body = Client().get("/api/v1/search/?s=zoo").json()
    page = Client().get("/?q=zoo").content.decode()

    assert [result["url"] for result in v1_body] == [WEB_RESULT.url]
    assert WEB_RESULT.url in page
    assert ZOO_RESULT.url not in page


@pytest.mark.django_db
def test_super_search_drops_a_file_the_store_lacks(monkeypatch):
    stored()
    _stub_sources(monkeypatch, {"hn": [ZOO_RESULT, PANDA_RESULT, WEB_RESULT]})
    _stub_scoring(monkeypatch, [0.0] * 20)
    monkeypatch.setattr(super_search, "crawl_url", lambda url, redis: {"content": None})
    emitted = []

    async def emit(event_type, data):
        emitted.append((event_type, data))

    all_docs: list[Document] = []
    async_to_sync(super_search._run_pipeline)("red panda", emit, all_docs, [None], asyncio.Lock())

    assert [document.url for document in all_docs] == [PANDA_RESULT.url, WEB_RESULT.url]


@pytest.mark.django_db
def test_super_search_caps_the_files_the_store_vouches_for_in_its_final_list(monkeypatch):
    images = [
        make_image(f"{name}.jpg", page_url=f"https://commons.wikimedia.org/wiki/File:{name}.jpg") for name in "ABC"
    ]
    for image in images:
        stored(image)
    results = [ZOO_RESULT] + [Document(image.title, image.page_url, "Crawled.") for image in images] + [WEB_RESULT]
    _stub_scoring(monkeypatch, [0.0] * 20)
    emitted = []

    async def emit(event_type, data):
        emitted.append((event_type, data))

    async_to_sync(super_search._emit_final_results)("", results, emit, [None], asyncio.Lock())

    [(_, final)] = emitted
    shown_files = [item.url for item in final.results if commons_file_key(item.url)]
    assert len(shown_files) == 2
    assert ZOO_RESULT.url not in shown_files
    assert WEB_RESULT.url in [item.url for item in final.results]
