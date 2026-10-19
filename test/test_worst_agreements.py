from datetime import timedelta

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from mwmbl.crawler.worst_agreements import (
    STORED,
    SUBMITTER,
    UNKNOWN_GAP,
    WORST_PER_BUCKET,
    agreement_score,
    gap_bucket,
    read_worst_agreements,
    record_worst_agreements,
)
from mwmbl.indexer.agreement import Agreement
from mwmbl.search_callers import window_start
from mwmbl.utils import utc_today

SUBMITTER_ID = 1
STORED_ID = 2


@pytest.fixture
def client():
    return fakeredis.FakeRedis(decode_responses=True)


def _today():
    """Not a fixed date: the keys expire a few weeks after the week they are for."""
    return utc_today()


def _window():
    return window_start(_today())


def _agreement(
    url="https://example.com/",
    title_similarity=1.0,
    extract_similarity=0.5,
    delta=60,
    extract="Spam",
    existing_extract="Real",
    existing_user_id=STORED_ID,
):
    return Agreement(
        url=url,
        existing_user_id=existing_user_id,
        title_similarity=title_similarity,
        extract_similarity=extract_similarity,
        extract_empty=not extract.strip(),
        existing_extract_empty=not existing_extract.strip(),
        last_crawled_delta=delta,
        title="Title",
        extract=extract,
        existing_title="Title",
        existing_extract=existing_extract,
    )


def _record(client, agreements, user_id=SUBMITTER_ID):
    record_worst_agreements(client, agreements, user_id, _today())


def _read(client, user_id, role, bucket="hour"):
    return read_worst_agreements(client, user_id, _window())[role][bucket]


@pytest.mark.parametrize(
    "delta, bucket",
    [
        (None, UNKNOWN_GAP.name),
        (0, "hour"),
        (-3599, "hour"),
        (3600, "day"),
        (-86400 * 3, "week"),
        (86400 * 30, "older"),
    ],
)
def test_gap_bucket(delta, bucket):
    assert gap_bucket(delta).name == bucket


def test_agreement_score_takes_the_lower_similarity():
    assert agreement_score(_agreement(title_similarity=0.2, extract_similarity=0.9)) == 0.2
    assert agreement_score(_agreement(title_similarity=0.9, extract_similarity=0.3)) == 0.3


def test_agreement_score_compares_titles_alone_when_an_extract_is_empty():
    assert agreement_score(_agreement(title_similarity=0.7, extract_similarity=0.0, extract="")) == 0.7


def test_keeps_the_comparison_under_both_crawlers(client):
    _record(client, [_agreement()])

    (submitted,) = _read(client, SUBMITTER_ID, SUBMITTER)
    (stored,) = _read(client, STORED_ID, STORED)

    assert submitted["other_user_id"] == STORED_ID
    assert (submitted["own"]["extract"], submitted["other"]["extract"]) == ("Spam", "Real")
    assert stored["other_user_id"] == SUBMITTER_ID
    assert (stored["own"]["extract"], stored["other"]["extract"]) == ("Real", "Spam")
    assert _read(client, SUBMITTER_ID, STORED) == []
    assert _read(client, STORED_ID, SUBMITTER) == []


def test_keeps_only_the_least_agreeing(client):
    agreements = [
        _agreement(url=f"https://example.com/{i}", extract_similarity=i / 100) for i in range(WORST_PER_BUCKET + 10)
    ]

    _record(client, agreements)

    kept = _read(client, SUBMITTER_ID, SUBMITTER)
    assert [entry["score"] for entry in kept] == [i / 100 for i in range(WORST_PER_BUCKET)]
    assert client.hlen(next(iter(client.keys("*:submitter:*:entries")))) == WORST_PER_BUCKET


def test_ranks_each_crawl_gap_separately(client):
    """A stale copy scoring lower does not push out a fresh one."""
    stale = [_agreement(url=f"https://example.com/{i}", extract_similarity=0.1, delta=86400 * 30) for i in range(60)]
    fresh = _agreement(url="https://example.com/fresh", extract_similarity=0.6, delta=60)

    _record(client, stale + [fresh])

    assert [entry["url"] for entry in _read(client, SUBMITTER_ID, SUBMITTER)] == ["https://example.com/fresh"]
    assert len(_read(client, SUBMITTER_ID, SUBMITTER, "older")) == WORST_PER_BUCKET


def test_other_crawlers_cannot_push_out_a_crawlers_own_submissions(client):
    _record(client, [_agreement(url="https://example.com/spam", extract_similarity=0.3)], user_id=SUBMITTER_ID)
    junk = [
        _agreement(url=f"https://example.com/{i}", extract_similarity=0.01, existing_user_id=SUBMITTER_ID)
        for i in range(WORST_PER_BUCKET)
    ]
    _record(client, junk, user_id=3)

    assert [entry["url"] for entry in _read(client, SUBMITTER_ID, SUBMITTER)] == ["https://example.com/spam"]
    assert len(_read(client, SUBMITTER_ID, STORED)) == WORST_PER_BUCKET


def test_a_url_compared_again_replaces_its_entry(client):
    for delta in range(60):
        _record(client, [_agreement(delta=delta, extract=f"Spam {delta}")])

    (kept,) = _read(client, SUBMITTER_ID, SUBMITTER)
    assert kept["own"]["extract"] == "Spam 59"


def test_keeps_empty_extracts_ranked_by_title(client):
    _record(client, [_agreement(title_similarity=0.4, extract="")])

    (kept,) = _read(client, SUBMITTER_ID, SUBMITTER)
    assert kept["score"] == 0.4


def test_keeps_each_week_apart(client):
    _record(client, [_agreement()])

    previous_week = read_worst_agreements(client, SUBMITTER_ID, _window() - timedelta(weeks=1))
    assert not any(entries for by_bucket in previous_week.values() for entries in by_bucket.values())


def test_keys_expire(client):
    _record(client, [_agreement()])

    keys = client.keys("crawler:worst-agreements:*")
    assert keys
    assert all(client.ttl(key) > 0 for key in keys)


@pytest.fixture
def admin_browser(db):
    admin = get_user_model().objects.create_superuser(username="admin", email="admin@example.com", password="pw")
    browser = Client()
    browser.force_login(admin)
    return browser


@pytest.mark.django_db
def test_admin_page_shows_both_copies(client, admin_browser, monkeypatch):
    User = get_user_model()
    submitter = User.objects.create_user(username="sneaky", email="sneaky@example.com", password="pw")
    other = User.objects.create_user(username="honest", email="honest@example.com", password="pw")
    agreement = _agreement(extract="Buy cheap pills", existing_extract="Learn python", existing_user_id=other.id)
    _record(client, [agreement], user_id=submitter.id)
    monkeypatch.setattr("mwmbl.crawler.worst_agreements.get_redis", lambda: client)

    response = admin_browser.get("/admin/worst-agreements/", {"username": "sneaky"})

    content = response.content.decode()
    assert response.status_code == 200
    assert "Buy cheap pills" in content
    assert "Learn python" in content
    assert "sneaky submitted over honest's copy" in content


@pytest.mark.django_db
def test_admin_page_with_an_unknown_user(admin_browser):
    response = admin_browser.get("/admin/worst-agreements/", {"username": "nobody"})

    assert "No user named nobody" in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("weeks_ago", ["abc", "-1", "4"])
def test_admin_page_rejects_a_week_it_does_not_keep(admin_browser, weeks_ago):
    response = admin_browser.get("/admin/worst-agreements/", {"username": "nobody", "weeks_ago": weeks_ago})

    assert response.status_code == 400
