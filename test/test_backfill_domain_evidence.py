"""
The evidence backfill: which submitted domains it picks, and that one failing domain does
not end the run.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from django.core.management import call_command

from mwmbl.management.commands import backfill_domain_evidence as backfill
from mwmbl.models import DomainEvidence, DomainSubmission, MwmblUser


@pytest.fixture
def submitter(db):
    return MwmblUser.objects.create_user(username="submitter", email="s@example.com", password="pw")


def _submit(submitter, name, status="PENDING", year=2025):
    return DomainSubmission.objects.create(
        name=name,
        status=status,
        submitted_by=submitter,
        submitted_on=datetime(year, 6, 1, tzinfo=timezone.utc),
    )


def _options(**overrides):
    return {"status": None, "since": None, "limit": None, **overrides}


@pytest.mark.django_db
def test_domains_already_crawled_duplicates_and_malformed_names_are_skipped(submitter):
    _submit(submitter, "new.example")
    _submit(submitter, "new.example")
    _submit(submitter, "done.example")
    _submit(submitter, "Bad-Case.example")
    DomainEvidence.objects.create(domain="done.example", state=DomainEvidence.State.READY)

    domains = backfill.Command._domains_to_crawl(_options())

    assert domains == ["new.example"]


@pytest.mark.django_db
def test_domains_can_be_filtered_by_status_date_and_limit(submitter):
    _submit(submitter, "old-approved.example", status="APPROVED", year=2023)
    _submit(submitter, "new-approved.example", status="APPROVED", year=2025)
    _submit(submitter, "new-pending.example", status="PENDING", year=2025)

    by_status = backfill.Command._domains_to_crawl(_options(status=["APPROVED"]))
    by_date = backfill.Command._domains_to_crawl(_options(since=date(2024, 1, 1)))
    limited = backfill.Command._domains_to_crawl(_options(limit=1))

    assert by_status == ["old-approved.example", "new-approved.example"]
    assert by_date == ["new-approved.example", "new-pending.example"]
    assert limited == ["old-approved.example"]


@pytest.mark.django_db
def test_one_failing_domain_is_recorded_and_the_run_continues(submitter, monkeypatch, capsys):
    _submit(submitter, "broken.example")
    _submit(submitter, "fine.example")
    stored, failures = [], []

    def crawl(domain, redis):
        if domain == "broken.example":
            raise TimeoutError("timed out")
        return domain

    def store(domain, crawled):
        stored.append(domain)
        return SimpleNamespace(suggested_action="APPROVE", state="READY")

    monkeypatch.setattr(backfill, "get_redis", lambda: None)
    monkeypatch.setattr(backfill, "crawl_domain", crawl)
    monkeypatch.setattr(backfill, "store_evidence", store)
    monkeypatch.setattr(backfill, "refresh_suggestion", lambda evidence: None)
    monkeypatch.setattr(backfill, "store_failure", lambda domain, reason: failures.append((domain, reason)))

    call_command("backfill_domain_evidence", "--delay", "0")

    captured = capsys.readouterr()
    assert stored == ["fine.example"]
    assert failures == [("broken.example", "TimeoutError")]
    assert "2 domains to crawl" in captured.out
    assert "fine.example: APPROVE (READY)" in captured.out
    assert "broken.example: FAILED (timed out)" in captured.err


@pytest.mark.django_db
def test_the_delay_between_domains_is_honoured(submitter, monkeypatch):
    _submit(submitter, "one.example")
    sleeps = []
    monkeypatch.setattr(backfill, "get_redis", lambda: None)
    monkeypatch.setattr(backfill, "crawl_domain", lambda domain, redis: domain)
    monkeypatch.setattr(
        backfill, "store_evidence", lambda domain, crawled: SimpleNamespace(suggested_action="", state="X")
    )
    monkeypatch.setattr(backfill, "refresh_suggestion", lambda evidence: None)
    monkeypatch.setattr(backfill.time, "sleep", sleeps.append)

    call_command("backfill_domain_evidence", "--delay", "2.5")

    assert sleeps == [2.5]
