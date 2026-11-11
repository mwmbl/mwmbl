"""Writing stored Commons files to the search index (mwmbl.imagesearch.indexing)."""

import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.core.management import call_command
from test_image_search import make_image
from test_image_store import IMAGE, URL, stored
from test_index_batches import PATCH_TARGET, _all_urls, snapshot_blacklist

from mwmbl.imagesearch import indexing, store
from mwmbl.indexer import index_batches
from mwmbl.models import CommonsImage
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

Reason = CommonsImage.RevocationReason
NUM_PAGES = 64
NOTHING = {"stored": 0, "dropped": 0, "skipped": 0}


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "images.tinysearch"
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=NUM_PAGES, page_size=4096)
    return str(path)


@pytest.fixture(autouse=True)
def no_blacklist():
    with patch(PATCH_TARGET, return_value=snapshot_blacklist(set())):
        yield


def written(index_path) -> list[Document]:
    with TinyIndex(item_factory=Document, index_path=index_path, mode="r") as index:
        return [document for page in range(NUM_PAGES) for document in index.get_page(page)]


def row(**fields) -> CommonsImage:
    defaults = dict(title="Red_panda.jpg", caption="", description="A red panda in a zoo")
    defaults.update(fields)
    return CommonsImage(page_url=URL, last_checked_at=datetime(2024, 5, 6, tzinfo=timezone.utc), **defaults)


def test_a_file_is_a_document_titled_by_its_caption_with_its_description_as_extract():
    document = indexing.image_document(row(caption="A red panda eating bamboo"))

    assert (document.title, document.url, document.extract) == (
        "A red panda eating bamboo",
        URL,
        "A red panda in a zoo",
    )
    assert document.last_crawled == int(datetime(2024, 5, 6, tzinfo=timezone.utc).timestamp())


def test_without_a_caption_the_title_is_the_file_name_without_extension_or_underscores():
    assert indexing.image_document(row(title="Red_panda  at_the.zoo.jpg")).title == "Red panda at the.zoo"


def test_title_and_extract_are_cut_to_the_crawlers_lengths():
    document = indexing.image_document(row(caption="c" * 100, description="d" * 1000))

    assert document.title == "c" * 64 + "…"
    assert document.extract == "d" * 154 + "…"


@pytest.mark.parametrize(
    "name",
    [
        "IMG_1234.jpg",
        "DSC_0045.JPG",
        "DSCN1234.jpg",
        "P1010001.JPG",
        "IMG-20230101-WA0001.jpg",
        "PXL_20230101_123456789.jpg",
        "20230101_123456.jpg",
        "2023-01-01 12.34.56 (2).jpg",
        "GOPR0001.MP4",
        "DSC_0045-edited.jpg",
        "IMG_E1234.jpg",
        "IMG_1234-HDR.jpg",
        "DSC01234-Enhanced-NR.jpg",
        "SANY0001.JPG",
        "VID_20230101.mp4",
        "IMG_20230101_123456~2.jpg",
        "Screenshot 2023-01-01 at 12.34.56.png",
        "WhatsApp Image 2023-01-01 at 12.34.56.jpeg",
    ],
)
def test_a_camera_or_date_file_name_alone_is_not_real_text(name):
    assert not indexing.has_real_text(row(title=name, description=""))


@pytest.mark.parametrize(
    "name",
    [
        "Red panda.jpg",
        "IMG of Big Ben.jpg",
        "Pilsner 1842.jpg",
        "DSC Mumbai 2019.jpg",
        "P-51.jpg",
        "747.jpg",
        "1984.jpg",
        "WP 40.jpg",
        "SAM 7.jpg",
        "Ottawa 2019.jpg",
    ],
)
def test_a_file_name_with_words_is_real_text(name):
    assert indexing.has_real_text(row(title=name, description=""))


def test_a_caption_makes_a_camera_file_name_real_text():
    assert indexing.has_real_text(row(title="IMG_1234.jpg", caption="Big Ben at night", description=""))


def test_a_camera_file_with_a_description_is_titled_by_the_descriptions_opening_words():
    description = "The Eiffel Tower lit up at night, seen from the Trocadéro gardens across the Seine in Paris"
    camera_file = row(title="IMG_1234.jpg", description=description)

    assert indexing.has_real_text(camera_file)
    assert indexing.image_document(camera_file).title == "The Eiffel Tower lit up at night, seen from the Trocadéro…"
    assert indexing.image_document(row(title="IMG_1234.jpg", description="Big Ben")).title == "Big Ben"


@pytest.mark.django_db
def test_approved_files_with_real_text_are_written_once(index_path):
    stored()
    camera_url = "https://commons.wikimedia.org/wiki/File:IMG_1234.jpg"
    camera = stored(make_image("IMG_1234.jpg", page_url=camera_url, description=""))

    first = indexing.index_images(index_path, 100)
    after_first = written(index_path)
    second = indexing.index_images(index_path, 100)

    assert first == {"stored": 1, "dropped": 0, "skipped": 1}
    assert {document.url for document in after_first} == {URL}
    # Under its own terms, as a crawled page is.
    assert {"panda", "zoo"} <= {document.term for document in after_first}
    assert second == NOTHING
    assert written(index_path) == after_first
    assert CommonsImage.objects.get(pk=camera.pk).indexed_text_hash == camera.text_hash


@pytest.mark.django_db
def test_a_file_is_written_again_when_its_text_changes(index_path):
    row = stored()
    indexing.index_images(index_path, 100)
    changed = dataclasses.replace(IMAGE, description="A red panda asleep in a tree")
    store.approve(row, changed, 0.25, row.nsfw_fingerprint, row.last_checked_at)
    row.save()

    counts = indexing.index_images(index_path, 100)

    assert counts == {"stored": 1, "dropped": 0, "skipped": 0}
    assert "asleep" in {document.term for document in written(index_path)}


@pytest.mark.django_db
def test_revoked_files_are_not_written(index_path):
    stored(revoked_at=datetime(2024, 1, 1, tzinfo=timezone.utc), revocation_reason=Reason.NSFW)

    assert indexing.index_images(index_path, 100) == NOTHING
    assert written(index_path) == []


@pytest.mark.django_db
def test_a_file_uploaded_too_recently_is_left_for_a_later_run(index_path, settings):
    row = stored(uploaded_at=datetime.now(timezone.utc) - timedelta(days=29))
    settings.IMAGE_INDEX_MIN_UPLOAD_AGE = timedelta(days=30)

    assert indexing.index_images(index_path, 100) == NOTHING
    assert CommonsImage.objects.get(pk=row.pk).indexed_text_hash == ""

    settings.IMAGE_INDEX_MIN_UPLOAD_AGE = timedelta(days=28)
    assert indexing.index_images(index_path, 100) == {"stored": 1, "dropped": 0, "skipped": 0}


@pytest.mark.django_db
def test_a_blacklisted_file_is_counted_as_dropped(index_path):
    stored()

    with patch(PATCH_TARGET, return_value=snapshot_blacklist({"commons.wikimedia.org"})):
        counts = indexing.index_images(index_path, 100)

    assert counts == {"stored": 0, "dropped": 1, "skipped": 0}
    assert written(index_path) == []


@pytest.mark.parametrize(
    "meanwhile",
    [{"text_hash": "changed"}, {"revoked_at": datetime(2024, 1, 1, tzinfo=timezone.utc), "revocation_reason": "nsfw"}],
)
@pytest.mark.django_db
def test_a_file_changed_or_revoked_during_the_run_is_not_marked_as_taken(index_path, monkeypatch, meanwhile):
    row = stored()

    def change_meanwhile(documents, path):
        CommonsImage.objects.filter(pk=row.pk).update(**meanwhile)
        return index_batches.index_documents(documents, path)

    monkeypatch.setattr(indexing, "index_documents", change_meanwhile)
    indexing.index_images(index_path, 100)

    assert CommonsImage.objects.get(pk=row.pk).indexed_text_hash == ""


@pytest.mark.django_db
def test_the_limit_bounds_a_run(index_path):
    stored()
    stored(make_image("Zoo.jpg", page_url="https://commons.wikimedia.org/wiki/File:Zoo.jpg"))
    one = {"stored": 1, "dropped": 0, "skipped": 0}

    assert indexing.index_images(index_path, 1) == one
    assert indexing.index_images(index_path, 1) == one
    assert indexing.index_images(index_path, 1) == NOTHING


@pytest.fixture
def configured_index(settings, tmp_path, index_path):
    settings.DATA_PATH = str(tmp_path)
    settings.INDEX_NAME = "images.tinysearch"
    return index_path


@pytest.mark.django_db
def test_the_command_writes_to_the_configured_index(settings, configured_index, capsys):
    settings.IMAGE_INDEXING_ENABLED = True
    stored()

    call_command("index_images")

    assert "stored: 1" in capsys.readouterr().out
    assert URL in _all_urls(configured_index, NUM_PAGES)


@pytest.mark.django_db
def test_the_command_writes_nothing_while_switched_off(settings, configured_index):
    settings.IMAGE_INDEXING_ENABLED = False
    stored()

    call_command("index_images")

    assert written(configured_index) == []
    assert CommonsImage.objects.get().indexed_text_hash == ""
