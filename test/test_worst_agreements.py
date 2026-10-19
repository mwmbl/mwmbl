from datetime import date, timedelta

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from mwmbl.crawler.worst_agreements import (
    UNKNOWN_GAP,
    WORST_PER_BUCKET,
    gap_bucket,
    read_worst_agreements,
    record_worst_agreements,
)
from mwmbl.indexer.agreement import Agreement
from mwmbl.search_callers import window_start
from mwmbl.utils import utc_today

TODAY = date(2026, 10, 21)
WINDOW = window_start(TODAY)
SUBMITTER_ID = 1
STORED_ID = 2


@pytest.fixture
def client():
    return fakeredis.FakeRedis(decode_responses=True)


def _agreement(url="https://example.com/", extract_similarity=0.5, delta=60, extract="Spam", existing_extract="Real"):
    return Agreement(
        url=url,
        existing_user_id=STORED_ID,
        title_similarity=1.0,
        extract_similarity=extract_similarity,
        extract_empty=not extract.strip(),
        existing_extract_empty=not existing_extract.strip(),
        last_crawled_delta=delta,
        title="Title",
        extract=extract,
        existing_title="Title",
        existing_extract=existing_extract,
    )


@pytest.mark.parametrize(
    "delta, bucket",
    [(None, UNKNOWN_GAP), (0, "hour"), (-3599, "hour"), (3600, "day"), (-86400 * 3, "week"), (86400 * 30, "older")],
)
def test_gap_bucket(delta, bucket):
    assert gap_bucket(delta) == bucket


def test_keeps_the_comparison_under_both_crawlers(client):
    record_worst_agreements(client, [_agreement()], SUBMITTER_ID, TODAY)

    (submitted,) = read_worst_agreements(client, SUBMITTER_ID, WINDOW)["hour"]
    (stored,) = read_worst_agreements(client, STORED_ID, WINDOW)["hour"]

    assert submitted["role"] == "submitter"
    assert submitted["other_user_id"] == STORED_ID
    assert (submitted["own"]["extract"], submitted["other"]["extract"]) == ("Spam", "Real")
    assert stored["role"] == "stored"
    assert stored["other_user_id"] == SUBMITTER_ID
    assert (stored["own"]["extract"], stored["other"]["extract"]) == ("Real", "Spam")


def test_keeps_only_the_least_agreeing(client):
    agreements = [
        _agreement(url=f"https://example.com/{i}", extract_similarity=i / 100) for i in range(WORST_PER_BUCKET + 10)
    ]

    record_worst_agreements(client, agreements, SUBMITTER_ID, TODAY)

    kept = read_worst_agreements(client, SUBMITTER_ID, WINDOW)["hour"]
    assert [entry["extract_similarity"] for entry in kept] == [i / 100 for i in range(WORST_PER_BUCKET)]


def test_ranks_each_crawl_gap_separately(client):
    """A stale copy scoring lower does not push out a fresh one."""
    stale = [_agreement(url=f"https://example.com/{i}", extract_similarity=0.1, delta=86400 * 30) for i in range(60)]
    fresh = _agreement(url="https://example.com/fresh", extract_similarity=0.6, delta=60)

    record_worst_agreements(client, stale + [fresh], SUBMITTER_ID, TODAY)

    kept = read_worst_agreements(client, SUBMITTER_ID, WINDOW)
    assert [entry["url"] for entry in kept["hour"]] == ["https://example.com/fresh"]
    assert len(kept["older"]) == WORST_PER_BUCKET


def test_skips_empty_extracts(client):
    record_worst_agreements(client, [_agreement(extract=""), _agreement(existing_extract=" ")], SUBMITTER_ID, TODAY)

    assert not any(read_worst_agreements(client, SUBMITTER_ID, WINDOW).values())


def test_keeps_each_week_apart(client):
    record_worst_agreements(client, [_agreement()], SUBMITTER_ID, TODAY)

    assert not any(read_worst_agreements(client, SUBMITTER_ID, WINDOW - timedelta(weeks=1)).values())


def test_keys_expire(client):
    record_worst_agreements(client, [_agreement()], SUBMITTER_ID, TODAY)

    assert all(client.ttl(key) > 0 for key in client.keys("crawler:worst-agreements:*"))


@pytest.mark.django_db
def test_admin_page_shows_both_copies(client):
    User = get_user_model()
    admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="pw")
    submitter = User.objects.create_user(username="sneaky", email="sneaky@example.com", password="pw")
    other = User.objects.create_user(username="honest", email="honest@example.com", password="pw")
    agreement = _agreement(extract="Buy cheap pills", existing_extract="Learn python")._replace(
        existing_user_id=other.id
    )
    record_worst_agreements(client, [agreement], submitter.id, utc_today())
    browser = Client()
    browser.force_login(admin)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("mwmbl.crawler.worst_agreements.get_redis", lambda: client)
        response = browser.get("/admin/worst-agreements/", {"username": "sneaky"})

    content = response.content.decode()
    assert response.status_code == 200
    assert "Buy cheap pills" in content
    assert "Learn python" in content
    assert "honest's copy" in content


@pytest.mark.django_db
def test_admin_page_with_an_unknown_user(client):
    admin = get_user_model().objects.create_superuser(username="admin", email="admin@example.com", password="pw")
    browser = Client()
    browser.force_login(admin)

    response = browser.get("/admin/worst-agreements/", {"username": "nobody"})

    assert "No user named nobody" in response.content.decode()
