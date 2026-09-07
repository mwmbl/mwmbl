import gzip
import json
from datetime import datetime, timezone

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from mwmbl.models import Curation, DomainSubmission, FlagCuration, SearchResultVote, UserCuration

User = get_user_model()


def doc(url, title="title", extract="extract", score=1.0, state=None, user_ids=None):
    return {
        "url": url,
        "title": title,
        "extract": extract,
        "score": score,
        "state": state,
        "term": "secret term",
        "user_ids": user_ids or [42],
    }


@pytest.fixture
def user():
    return User.objects.create_user(username="curator", email="c@example.com", password="x")


@pytest.fixture
def flagger():
    return User.objects.create_user(username="flagger", email="f@example.com", password="x")


@pytest.fixture
def curation(user):
    return Curation.objects.create(
        user=user,
        timestamp=datetime(2025, 3, 1, tzinfo=timezone.utc),
        query="test query",
        original_index_results=[doc("https://a.com")],
        original_results=[doc("https://a.com"), doc("https://b.com")],
        new_results=[doc("https://b.com"), doc("https://a.com")],
        num_changes=1,
    )


@pytest.mark.django_db
def test_export_writes_sanitized_records(tmp_path, user, flagger, curation):
    FlagCuration.objects.create(
        user=flagger,
        timestamp=datetime(2025, 3, 2, tzinfo=timezone.utc),
        curation=curation,
        flag="PROMOTION",
        status="ACCEPTED",
        reason="self promo",
    )
    UserCuration.objects.create(
        user=user,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        url="https://mwmbl.org/?q=old+query",
        results=[doc("https://c.com")],
        curation_type="curate_delete",
        curation={"delete_index": 0},
    )
    SearchResultVote.objects.create(user=user, url="https://a.com", query="test query", vote_type="upvote")
    DomainSubmission.objects.create(
        name="spammy.example.com",
        submitted_by=user,
        status="REJECTED",
        status_changed_by=flagger,
        rejection_reason="SPAM",
        rejection_detail="link farm",
    )

    call_command("export_judgments", output_dir=str(tmp_path))

    records = {}
    for name in ("curations", "user_curations", "votes", "domains"):
        with gzip.open(tmp_path / f"{name}.jsonl.gz", "rt") as f:
            records[name] = [json.loads(line) for line in f]

    [exported] = records["curations"]
    assert exported["query"] == "test query"
    assert exported["flags"] == [{"flag": "PROMOTION", "status": "ACCEPTED"}]
    assert [d["url"] for d in exported["new_results"]] == ["https://b.com", "https://a.com"]
    # documents are sanitized: no user_ids or term, no real user identity anywhere
    for document in exported["original_results"] + exported["new_results"]:
        assert set(document) == {"url", "title", "extract", "score", "state"}
    assert exported["user"].startswith("u")
    assert "curator" not in json.dumps(records)

    [event] = records["user_curations"]
    assert event["query"] == "old query"
    assert event["curation_type"] == "curate_delete"
    assert event["curation"] == {"delete_index": 0}
    # same user gets the same opaque id across files
    assert event["user"] == exported["user"]

    [vote] = records["votes"]
    assert vote["vote_type"] == "upvote"
    assert vote["user"] == exported["user"]

    [domain] = records["domains"]
    assert domain["domain"] == "spammy.example.com"
    assert domain["status"] == "REJECTED"
    assert domain["rejection_reason"] == "SPAM"
    assert domain["user"] == exported["user"]
    # the reviewing moderator's identity is not exported
    assert "flagger" not in json.dumps(records)

    stats = json.loads((tmp_path / "stats.json").read_text())
    assert stats["curations"]["count"] == 1
    assert stats["curations"]["flag_status_counts"] == {"ACCEPTED": 1}
    assert stats["user_curations"]["curation_type_counts"] == {"curate_delete": 1}
    assert stats["votes"]["vote_type_counts"] == {"upvote": 1}
    assert stats["domains"]["status_counts"] == {"REJECTED": 1}
    assert stats["domains"]["rejection_reason_counts"] == {"SPAM": 1}
    assert stats["users"]["distinct_users"] == 1


@pytest.mark.django_db
def test_stats_only_writes_no_files(tmp_path, curation):
    call_command("export_judgments", output_dir=str(tmp_path / "out"), stats_only=True)
    assert not (tmp_path / "out").exists()


@pytest.mark.django_db
def test_since_filters_old_records(tmp_path, user, curation):
    Curation.objects.create(
        user=user,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        query="recent query",
        original_index_results=[],
        original_results=[],
        new_results=[],
        num_changes=1,
    )

    call_command("export_judgments", output_dir=str(tmp_path), since="2026-01-01")

    with gzip.open(tmp_path / "curations.jsonl.gz", "rt") as f:
        exported = [json.loads(line) for line in f]
    assert [c["query"] for c in exported] == ["recent query"]


@pytest.mark.django_db
def test_anonymous_and_malformed_data(tmp_path):
    Curation.objects.create(
        user=None,
        timestamp=datetime(2025, 5, 1, tzinfo=timezone.utc),
        query="anon query",
        original_index_results=[],
        original_results="garbage",
        new_results=[doc("https://a.com")],
        num_changes=1,
    )

    call_command("export_judgments", output_dir=str(tmp_path))

    with gzip.open(tmp_path / "curations.jsonl.gz", "rt") as f:
        [exported] = [json.loads(line) for line in f]
    assert exported["user"] is None
    assert exported["original_results"] == []
    assert [d["url"] for d in exported["new_results"]] == ["https://a.com"]
