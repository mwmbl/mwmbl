"""
The rows a domain-moderation model is trained on: real decisions, blocklist-derived
rejections for reasons with little real data, and hand-written seed examples.
"""

import json
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from mwmbl.models import DomainEvidence, DomainSubmission, MwmblUser
from mwmbl.moderation import training_data
from mwmbl.moderation.training_data import (
    DERIVED,
    REAL,
    SEED,
    TrainingRow,
    derived_rows,
    is_trainable_domain,
    real_rows,
    seed_rows,
)


@pytest.mark.parametrize(
    "domain, trainable",
    [
        ("example.com", True),
        ("", False),
        ("Example.com", False),
        ("https://example.com", False),
        ("not a domain", False),
    ],
)
def test_only_well_formed_lowercase_domains_are_trainable(domain, trainable):
    assert is_trainable_domain(domain) is trainable


@pytest.fixture
def submitter(db):
    return MwmblUser.objects.create_user(username="submitter", email="s@example.com", password="pw")


def _submission(submitter, name, status, **extra):
    return DomainSubmission.objects.create(
        name=name, status=status, submitted_by=submitter, submitted_on=timezone.now(), **extra
    )


@pytest.mark.django_db
def test_real_rows_join_decisions_to_crawled_page_text(submitter):
    _submission(submitter, "good.example", "APPROVED")
    _submission(submitter, "bad.example", "REJECTED", rejection_reason="SPAM")
    _submission(submitter, "undecided.example", "PENDING")
    _submission(submitter, "Bad-Case.example", "APPROVED")
    DomainEvidence.objects.create(
        domain="bad.example",
        state=DomainEvidence.State.READY,
        pages=[{"title": "Buy now", "extract": "cheap pills"}],
    )
    DomainEvidence.objects.create(domain="good.example", state=DomainEvidence.State.PENDING, pages=[{"title": "x"}])

    rows = {row.domain: row for row in real_rows()}

    assert set(rows) == {"good.example", "bad.example"}
    assert rows["bad.example"].rejected is True
    assert rows["bad.example"].reason == "SPAM"
    assert rows["bad.example"].page_texts == ["Buy now cheap pills"]
    assert rows["bad.example"].source == REAL
    assert rows["bad.example"].submitter == str(submitter.id)
    assert rows["good.example"].rejected is False
    assert rows["good.example"].reason == ""
    assert rows["good.example"].page_texts == []


def test_a_row_becomes_a_model_example():
    row = TrainingRow(domain="a.example", rejected=True, reason="SPAM", source=REAL, page_texts=["text"])

    example = row.to_example()

    assert example.domain == "a.example"
    assert example.page_texts == ["text"]


BLOCKLIST = """\
# a comment
0.0.0.0 spam-one.example
127.0.0.1 spam-two.example # trailing comment
plain-three.example
sub.domain.example
has_underscore.example
toolongtoolongtoolongtoolongtoolongtoolong.example

"""


@pytest.fixture
def blocklist(monkeypatch):
    response = MagicMock(text=BLOCKLIST)
    get = MagicMock(return_value=response)
    monkeypatch.setattr(training_data.requests, "get", get)
    return get


def test_apex_domains_are_read_from_hosts_and_plain_formats(blocklist):
    domains = training_data._fetch_apex_domains("https://blocklist.test/list")

    assert domains == ["spam-one.example", "spam-two.example", "plain-three.example"]
    blocklist.return_value.raise_for_status.assert_called_once()


def test_derived_rows_exclude_known_domains_and_cap_each_reason(blocklist):
    rows = derived_rows(["SPAM", "OFFENSIVE"], per_reason=1, exclude={"spam-one.example"})

    assert [row.reason for row in rows] == ["SPAM", "OFFENSIVE"]
    assert all(row.source == DERIVED and row.rejected for row in rows)
    assert all(row.domain != "spam-one.example" for row in rows)


def test_derived_rows_are_reproducible_for_a_seed(blocklist):
    first = derived_rows(["SPAM"], per_reason=2, exclude=set(), seed=7)
    second = derived_rows(["SPAM"], per_reason=2, exclude=set(), seed=7)

    assert [row.domain for row in first] == [row.domain for row in second]


def test_reasons_without_a_source_are_skipped(blocklist):
    assert derived_rows(["OTHER"], per_reason=5, exclude=set()) == []
    blocklist.assert_not_called()


def test_seed_rows_skip_blank_lines_and_comments(tmp_path):
    path = tmp_path / "seed.jsonl"
    path.write_text(
        "# hand-written\n\n"
        + json.dumps({"domain": "a.example", "status": "REJECTED", "reason": "SPAM", "text": "buy now"})
        + "\n"
        + json.dumps({"domain": "b.example", "status": "APPROVED"})
        + "\n"
    )

    rows = seed_rows(path)

    assert [(row.domain, row.rejected, row.reason, row.page_texts, row.source) for row in rows] == [
        ("a.example", True, "SPAM", ["buy now"], SEED),
        ("b.example", False, "", [], SEED),
    ]


def test_missing_seed_file_means_no_seed_rows(tmp_path):
    assert seed_rows(tmp_path / "missing.jsonl") == []
