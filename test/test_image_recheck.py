"""Rechecking stored Commons files, with Commons and the judge stubbed (no network)."""

import dataclasses
import logging
from datetime import datetime, timedelta, timezone

import pytest
from django.core.management import call_command
from test_image_search import make_page
from test_image_store import IMAGE, LONG_AGO, OTHER, URL, stored

from mwmbl.imagesearch import commons, judge, recheck
from mwmbl.imagesearch.types import ImageSourceUnavailable
from mwmbl.models import CommonsImage

Reason = CommonsImage.RevocationReason


def test_fetch_files_asks_for_every_title_at_once_and_leaves_out_missing_files(httpx_mock):
    found = make_page("Red panda é.jpg", 0)
    del found["index"]
    pages = [found, {"title": "File:Zoo.jpg", "missing": True}, {"title": "File:Bad.jpg"}]
    httpx_mock.add_response(json={"query": {"pages": pages}})
    bad = "https://commons.wikimedia.org/wiki/File:Bad.jpg"

    with commons.new_client() as client:
        files = commons.fetch_files(client, [URL, OTHER.page_url, bad])

    assert files.keys() == {URL, bad}
    assert files[URL].sha1 == "sha1-of-Red panda é.jpg"
    assert files[bad] is None
    assert httpx_mock.get_requests()[0].url.params["titles"] == "File:Red_panda_é.jpg|File:Zoo.jpg|File:Bad.jpg"


@pytest.fixture
def commons_now(monkeypatch):
    """Stub Commons: `files` is what Commons has now, by page URL; `fail` makes it back off; `during` runs while
    a batch is fetched, as another process might."""
    state = dataclasses.make_dataclass("Commons", ["files", "fail", "requests", "during"])({}, False, [], None)

    def fake_fetch(client, page_urls):
        state.requests.append(page_urls)
        if state.fail:
            raise ImageSourceUnavailable("Wikimedia asked us to back off")
        if state.during:
            state.during()
        return {page_url: state.files[page_url] for page_url in page_urls if page_url in state.files}

    monkeypatch.setattr(commons, "fetch_files", fake_fetch)
    return state


@pytest.fixture
def nsfw(monkeypatch):
    """Stub the judge: every image it is asked about gets `nsfw.score`, or it raises `nsfw.error`."""
    state = dataclasses.make_dataclass("Judge", ["score", "error", "asked"])(0.0, None, [])

    def fake_scores(images):
        state.asked.extend(images)
        if state.error:
            raise state.error
        return [state.score for _ in images]

    monkeypatch.setattr(judge, "nsfw_scores", fake_scores)
    return state


def rechecked(row: CommonsImage) -> CommonsImage:
    recheck.recheck(10)
    row.refresh_from_db()
    return row


@pytest.mark.django_db
def test_an_unchanged_file_is_updated_without_asking_the_judge(commons_now, nsfw):
    row = stored()
    commons_now.files[URL] = dataclasses.replace(IMAGE, thumbnail_width=300)

    row = rechecked(row)

    assert (row.revoked_at, row.thumbnail_width, row.nsfw_score) == (None, 300, 0.25)
    assert row.last_checked_at > row.approved_at
    assert nsfw.asked == []


@pytest.mark.parametrize(
    ("file", "reason"),
    [
        (None, Reason.UNUSABLE),
        (dataclasses.replace(IMAGE, page_url="https://commons.wikimedia.org/wiki/File:Renamed.jpg"), Reason.GONE),
        (dataclasses.replace(IMAGE, categories=("Deletion requests November 2026",)), Reason.NOMINATED),
        (dataclasses.replace(IMAGE, description="A nude red panda"), Reason.RULES),
        (dataclasses.replace(IMAGE, sha1="new", revised_at=datetime.now(timezone.utc)), Reason.UNSETTLED),
    ],
)
@pytest.mark.django_db
def test_a_file_that_fails_a_check_is_revoked(commons_now, nsfw, file, reason):
    row = stored()
    commons_now.files[URL] = file

    row = rechecked(row)

    assert row.revocation_reason == reason
    assert row.revoked_at is not None


@pytest.mark.django_db
def test_a_deleted_file_is_revoked(commons_now, nsfw):
    assert rechecked(stored()).revocation_reason == Reason.GONE


@pytest.mark.django_db
def test_a_denied_file_is_revoked(commons_now, nsfw, settings):
    settings.IMAGE_SEARCH_DENIED_FILES = {IMAGE.sha1.lower()}
    commons_now.files[URL] = IMAGE

    assert rechecked(stored()).revocation_reason == Reason.DENIED


@pytest.mark.parametrize(
    "changed",
    [dataclasses.replace(IMAGE, sha1="new"), dataclasses.replace(IMAGE, description="A red panda asleep")],
)
@pytest.mark.django_db
def test_changed_content_is_judged_again_and_revoked_if_it_fails(commons_now, nsfw, changed):
    commons_now.files[URL] = changed
    nsfw.score = 1.0

    row = rechecked(stored())

    assert nsfw.asked == [changed]
    assert row.revocation_reason == Reason.NSFW


@pytest.mark.django_db
def test_content_revoked_as_nsfw_is_not_judged_again_while_unchanged(commons_now, nsfw):
    changed = dataclasses.replace(IMAGE, sha1="new")
    commons_now.files[URL] = changed
    nsfw.score = 1.0
    rechecked(stored())
    nsfw.score = 0.0

    row = rechecked(CommonsImage.objects.get())

    assert nsfw.asked == [changed]
    assert row.revocation_reason == Reason.NSFW


@pytest.mark.django_db
def test_changed_content_that_passes_is_approved_afresh(commons_now, nsfw):
    commons_now.files[URL] = dataclasses.replace(IMAGE, sha1="new")
    nsfw.score = 0.5

    row = rechecked(stored())

    assert (row.revoked_at, row.sha1, row.nsfw_score) == (None, "new", 0.5)
    assert row.approved_at == row.last_checked_at


@pytest.mark.parametrize(("score", "reason"), [(0.5, ""), (2.0, Reason.NSFW)])
@pytest.mark.django_db
def test_a_score_from_another_model_or_question_is_asked_again(commons_now, nsfw, score, reason):
    commons_now.files[URL] = IMAGE
    nsfw.score = score

    row = rechecked(stored(nsfw_fingerprint="old"))

    assert nsfw.asked == [IMAGE]
    assert row.revocation_reason == reason


@pytest.mark.django_db
def test_a_judge_failure_revokes_the_batch_until_a_later_pass(commons_now, nsfw):
    commons_now.files[URL] = IMAGE
    nsfw.error = ValueError("Score off the scale")
    row = rechecked(stored(nsfw_fingerprint="old"))
    assert row.revocation_reason == Reason.UNJUDGED

    nsfw.error = None
    assert rechecked(row).revoked_at is None


@pytest.mark.django_db
def test_a_revoked_file_stays_revoked_on_a_fresh_pass_of_unchanged_content(commons_now, nsfw):
    commons_now.files[URL] = IMAGE

    row = rechecked(stored(revoked_at=LONG_AGO, revocation_reason=Reason.RULES, nsfw_fingerprint="old"))

    assert (row.revoked_at, row.revocation_reason) == (LONG_AGO, Reason.RULES)


@pytest.mark.django_db
def test_a_revoked_file_whose_content_changed_and_passes_is_restored(commons_now, nsfw):
    commons_now.files[URL] = dataclasses.replace(IMAGE, sha1="new")

    row = rechecked(stored(revoked_at=LONG_AGO, revocation_reason=Reason.NSFW))

    assert (row.revoked_at, row.revocation_reason, row.sha1) == (None, "", "new")


@pytest.mark.django_db
def test_operator_revocations_are_not_rechecked_even_if_made_during_a_run(commons_now, nsfw):
    stored(revoked_at=LONG_AGO, revocation_reason=Reason.OPERATOR, page_url=OTHER.page_url)
    row = stored()
    commons_now.files[URL] = IMAGE
    commons_now.during = lambda: CommonsImage.objects.filter(pk=row.pk).update(
        revoked_at=LONG_AGO, revocation_reason=Reason.OPERATOR
    )

    row = rechecked(row)

    assert commons_now.requests == [[URL]]
    assert row.revocation_reason == Reason.OPERATOR


@pytest.mark.django_db
def test_a_run_rechecks_the_files_checked_longest_ago_up_to_its_limit(commons_now, nsfw, monkeypatch):
    monkeypatch.setattr(commons, "FILES_PER_REQUEST", 2)
    urls = [f"https://commons.wikimedia.org/wiki/File:{n}.jpg" for n in range(5)]
    for days, url in enumerate(urls):
        stored(dataclasses.replace(IMAGE, page_url=url), last_checked_at=LONG_AGO - timedelta(days))

    recheck.recheck(3)

    assert commons_now.requests == [[urls[4], urls[3]], [urls[2]]]


@pytest.mark.django_db
def test_a_back_off_stops_the_run_and_leaves_the_files_as_they_were(commons_now, nsfw, caplog, capsys):
    row = stored()
    commons_now.fail = True

    with caplog.at_level(logging.WARNING):
        call_command("recheck_images")

    assert "left unchecked: 1" in capsys.readouterr().out
    assert CommonsImage.objects.get().last_checked_at == row.last_checked_at
    assert "Stopped rechecking images" in caplog.text
