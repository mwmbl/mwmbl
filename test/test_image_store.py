"""The store of judged Commons files and their keys (no network)."""

import dataclasses
from datetime import datetime, timezone

import pytest
from test_image_search import make_image

from mwmbl.imagesearch import judge, store
from mwmbl.imagesearch.commons import commons_file_key
from mwmbl.models import CommonsImage

Reason = CommonsImage.RevocationReason
URL = "https://commons.wikimedia.org/wiki/File:Red_panda_%C3%A9.jpg"
IMAGE = make_image("Red panda é.jpg", page_url=URL)
OTHER = make_image("Zoo.jpg", page_url="https://commons.wikimedia.org/wiki/File:Zoo.jpg")
LONG_AGO = datetime(2023, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "url",
    [
        URL,
        "https://commons.wikimedia.org/wiki/File:Red panda é.jpg",
        "https://commons.m.wikimedia.org/wiki/File:Red_panda_é.jpg",
        "https://commons.wikimedia.org/wiki/File:red__panda_%C3%A9.jpg",
        "https://commons.wikimedia.org/wiki/Image:Red_panda_%C3%A9.jpg",
        "https://commons.wikimedia.org/wiki/file:_Red_panda_é.jpg",
        "https://commons.wikimedia.org/w/index.php?title=File:Red_panda_%C3%A9.jpg&action=history",
        "http://commons.wikimedia.org/w/index.php?title=Image%3Ared+panda+%C3%A9.jpg",
    ],
)
def test_a_commons_file_has_one_key_however_its_url_is_written(url):
    assert commons_file_key(url) == URL


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/wiki/File:Red_panda.jpg",
        "https://commons.wikimedia.org/wiki/Category:Red_pandas",
        "https://commons.wikimedia.org/wiki/File:",
        "https://commons.wikimedia.org/w/index.php?search=red+panda",
    ],
)
def test_anything_but_a_commons_file_page_has_no_key(url):
    assert commons_file_key(url) is None


def stored(image=IMAGE, **overrides) -> CommonsImage:
    row = CommonsImage(page_url=commons_file_key(image.page_url))
    store.approve(row, image, 0.25, judge.nsfw_fingerprint(), datetime(2022, 1, 1, tzinfo=timezone.utc))
    for name, value in overrides.items():
        setattr(row, name, value)
    row.save()
    return row


@pytest.mark.django_db
def test_every_image_that_passed_nsfw_is_recorded_on_the_day_and_no_other():
    long_description = dataclasses.replace(IMAGE, description="x" * 5000)

    store.record_judgments([long_description, OTHER], [0.25, 1.0])

    row = CommonsImage.objects.get()
    assert row.page_url == URL
    assert (row.sha1, row.text_hash, row.nsfw_score) == (IMAGE.sha1, store.text_hash(long_description), 0.25)
    assert row.nsfw_fingerprint == judge.nsfw_fingerprint()
    # Only the day, so the row can't be matched to the request that found the file.
    assert row.approved_at == row.last_checked_at
    assert (row.approved_at.hour, row.approved_at.minute, row.approved_at.second) == (0, 0, 0)
    # Only what the judge read is kept.
    assert len(row.description) == 1500
    assert row.revoked_at is None


@pytest.mark.django_db
def test_recording_an_unchanged_file_again_keeps_when_it_was_approved():
    approved_at = stored().approved_at

    store.record_judgments([dataclasses.replace(IMAGE, license_name="CC0")], [0.5])

    row = CommonsImage.objects.get()
    assert row.approved_at == approved_at
    assert (row.license_name, row.nsfw_score) == ("CC0", 0.5)


@pytest.mark.django_db
def test_a_stored_file_that_now_fails_is_revoked_for_its_new_content():
    stored()
    changed = dataclasses.replace(IMAGE, sha1="new")

    store.record_judgments([changed], [2.0])

    row = CommonsImage.objects.get()
    assert row.revocation_reason == Reason.NSFW
    assert row.sha1 == "new"


@pytest.mark.django_db
def test_recording_lifts_a_revocation_only_for_changed_content():
    stored(revoked_at=LONG_AGO, revocation_reason=Reason.NSFW)
    store.record_judgments([IMAGE], [0.0])
    assert CommonsImage.objects.get().revocation_reason == Reason.NSFW

    store.record_judgments([dataclasses.replace(IMAGE, sha1="new")], [0.0])
    row = CommonsImage.objects.get()
    assert (row.revoked_at, row.revocation_reason, row.sha1) == (None, "", "new")


@pytest.mark.django_db
def test_recording_lifts_a_revocation_that_said_nothing_about_the_content():
    stored(revoked_at=LONG_AGO, revocation_reason=Reason.UNJUDGED)

    store.record_judgments([IMAGE], [0.0])

    assert CommonsImage.objects.get().revoked_at is None


@pytest.mark.parametrize("score", [0.0, 2.0])
@pytest.mark.django_db
def test_recording_never_touches_an_operator_revocation(score):
    stored(revoked_at=LONG_AGO, revocation_reason=Reason.OPERATOR)

    store.record_judgments([dataclasses.replace(IMAGE, sha1="new")], [score])

    row = CommonsImage.objects.get()
    assert (row.revocation_reason, row.sha1) == (Reason.OPERATOR, IMAGE.sha1)


@pytest.mark.django_db
def test_a_failed_write_is_logged_without_its_message(monkeypatch, caplog):
    def fail(images, scores):
        raise RuntimeError(f"Key (page_url)=({URL}) already exists")

    monkeypatch.setattr(store, "record_judgments", fail)

    store.record_safely([IMAGE], [0.0])

    assert "Failed to record judged images: RuntimeError" in caplog.text
    assert URL not in caplog.text
