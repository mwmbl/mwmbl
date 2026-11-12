"""Writing the Commons files Seed Search judged safe to the search index (mwmbl.imagesearch.indexing)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from test_image_search import make_image
from test_index_batches import PATCH_TARGET, snapshot_blacklist

from mwmbl.imagesearch import indexing, seed_images
from mwmbl.imagesearch.prompt import NSFW_THRESHOLD
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, TinyIndex

NUM_PAGES = 64
NOW = datetime(2024, 5, 6, tzinfo=timezone.utc)
URL = "https://commons.wikimedia.org/wiki/File:Red_panda.jpg"
SAFE = 0.0
UNSAFE = NSFW_THRESHOLD + 1


def image(title="File:Red_panda.jpg", **fields):
    return make_image(title, page_url=URL, **fields)


@pytest.fixture
def index_path(tmp_path, settings):
    path = tmp_path / "index.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=NUM_PAGES, page_size=4096)
    settings.DATA_PATH = str(tmp_path)
    settings.INDEX_NAME = path.name
    with patch(PATCH_TARGET, return_value=snapshot_blacklist(set())):
        yield str(path)


def written(index_path) -> list[Document]:
    with TinyIndex(item_factory=Document, index_path=index_path, mode="r") as index:
        return [document for page in range(NUM_PAGES) for document in index.get_page(page)]


def test_a_file_is_a_document_titled_by_its_caption_with_its_licence_as_its_source():
    document = indexing.image_document(image(caption="A red panda eating bamboo"), NOW)

    assert (document.title, document.url, document.extract) == (
        "A red panda eating bamboo",
        URL,
        "A red panda in a zoo",
    )
    assert document.source == DocumentSource.COMMONS_CC_BY_SA_4_0
    assert document.last_crawled == int(NOW.timestamp())


def test_without_a_caption_the_title_is_the_file_name_without_namespace_extension_or_underscores():
    assert indexing.image_document(image("File:Red_panda  at_the.zoo.jpg"), NOW).title == "Red panda at the.zoo"


def test_title_and_extract_are_cut_to_the_crawlers_lengths():
    document = indexing.image_document(image(caption="c" * 100, description="d" * 1000), NOW)

    assert document.title == "c" * 64 + "…"
    assert document.extract == "d" * 154 + "…"


@pytest.mark.parametrize(
    "name",
    ["IMG_1234.jpg", "DSC_0045.JPG", "P1010001.JPG", "IMG-20230101-WA0001.jpg", "20230101_123456.jpg", "IMG_E1234.jpg"],
)
def test_a_camera_or_date_file_name_alone_is_not_real_text(name):
    assert not indexing.has_real_text(image(f"File:{name}", description=""))


@pytest.mark.parametrize("name", ["Red panda.jpg", "IMG of Big Ben.jpg", "P-51.jpg", "747.jpg", "Ottawa 2019.jpg"])
def test_a_file_name_with_words_is_real_text(name):
    assert indexing.has_real_text(image(f"File:{name}", description=""))


def test_a_camera_file_with_a_description_is_titled_by_the_descriptions_opening_words():
    description = "The Eiffel Tower lit up at night, seen from the Trocadéro gardens across the Seine in Paris"
    camera_file = image("File:IMG_1234.jpg", description=description)

    assert indexing.has_real_text(camera_file)
    assert (
        indexing.image_document(camera_file, NOW).title == "The Eiffel Tower lit up at night, seen from the Trocadéro…"
    )


def test_files_with_no_real_text_or_uploaded_recently_are_not_written(index_path):
    camera = image("File:IMG_1234.jpg", description="")
    recent = make_image("File:Recent.jpg", page_url=URL, uploaded_at=datetime.now(timezone.utc))

    assert indexing.index_images([camera, recent]) == 0
    assert written(index_path) == []


def test_a_written_file_keeps_its_licence_in_the_index_under_its_own_terms(index_path):
    assert indexing.index_images([image()]) == 1

    documents = written(index_path)
    assert {"panda", "zoo"} <= {document.term for document in documents}
    assert {document.url for document in documents} == {URL}
    assert {document.source for document in documents} == {DocumentSource.COMMONS_CC_BY_SA_4_0}


def test_a_blocked_file_is_not_written(index_path):
    blocked = snapshot_blacklist(set())
    with patch.object(blocked, "filter_blocked_urls", return_value={URL}), patch(PATCH_TARGET, return_value=blocked):
        assert indexing.index_images([image()]) == 0
    assert written(index_path) == []


def run_search(candidates, scores):
    """seed_images.start's phase 1 for a query, returning what the index write was handed."""
    handed = []

    async def search():
        with (
            patch.object(seed_images, "find_candidates", return_value=candidates),
            patch.object(seed_images.judge, "nsfw_scores", return_value=scores),
            patch.object(seed_images.indexing, "index_images", side_effect=handed.append),
        ):
            image_search = seed_images.start("a secret query")
            await image_search.nsfw_scores
            for _ in range(100):
                if handed:
                    break
                await asyncio.sleep(0.01)

    asyncio.run(search())
    return handed


def test_only_files_the_nsfw_judge_passed_are_handed_to_the_index(settings):
    settings.IMAGE_INDEXING_ENABLED = True
    safe, unsafe = image(), image("File:Other.jpg")

    assert run_search([safe, unsafe], [SAFE, UNSAFE]) == [[safe]]


def test_nothing_is_handed_to_the_index_while_indexing_is_off(settings):
    settings.IMAGE_INDEXING_ENABLED = False

    assert run_search([image()], [SAFE]) == []


def test_the_query_never_reaches_the_indexing_code(settings):
    settings.IMAGE_INDEXING_ENABLED = True

    handed = run_search([image()], [SAFE])

    assert handed
    assert "secret query" not in repr(handed)


def test_a_failed_index_write_is_logged_by_type_alone_and_does_not_raise(caplog):
    with patch.object(seed_images.indexing, "index_images", side_effect=OSError("File:Secret.jpg")):
        seed_images.write_to_index([image()])

    assert "OSError" in caplog.text
    assert "Secret" not in caplog.text


def test_a_failed_nsfw_judgment_writes_nothing(settings):
    settings.IMAGE_INDEXING_ENABLED = True

    async def search():
        with (
            patch.object(seed_images, "find_candidates", return_value=[image()]),
            patch.object(seed_images.judge, "nsfw_scores", side_effect=RuntimeError),
            patch.object(seed_images.indexing, "index_images") as index_images,
        ):
            image_search = seed_images.start("query")
            await asyncio.wait([image_search.nsfw_scores])
            await asyncio.sleep(0.05)
            return index_images.called

    assert not asyncio.run(search())


def test_the_minimum_upload_age_is_thirty_days(settings):
    assert settings.IMAGE_INDEX_MIN_UPLOAD_AGE == timedelta(days=30)
