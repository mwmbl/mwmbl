"""Tests for the 0037 repair of submissions holding a moderation *action* as their status.

An external moderation client posted "APPROVE"/"REJECT" where the column takes
"APPROVED"/"REJECTED", and nothing stopped it: Django checks `choices` in full_clean(),
which save() never calls, and emits no database constraint. The decisions became invisible
to every query that looks for a decided submission - the queue, the history, the curated
domains, the training data. These cover both halves of the fix: the rows are repaired, and
the column now refuses the bad value at the database, whatever writes it.
"""
import importlib

import pytest
from django.apps import apps as django_apps
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import override_settings

from mwmbl.curated_domains import CURATED_DOMAINS_CACHE_KEY, get_curated_domains
from mwmbl.models import DomainSubmission, MwmblUser

migration_module = importlib.import_module(
    "mwmbl.migrations.0037_repair_domain_submission_statuses")
repair_statuses = migration_module.repair_statuses

STATUS_CONSTRAINT = "domain_submission_status_valid"


@pytest.fixture
def user(db):
    return MwmblUser.objects.create(username="moderator")


@pytest.fixture
def broken_column(db):
    """Drop the status constraint for the duration of one test.

    The rows this migration repairs are rows the database now refuses, so they cannot be
    written any other way. Postgres DDL is transactional and pytest-django wraps each test
    in a transaction, so the constraint comes back on rollback.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"ALTER TABLE {DomainSubmission._meta.db_table} DROP CONSTRAINT {STATUS_CONSTRAINT}")
    yield


@pytest.fixture(autouse=True)
def clear_curated_cache():
    cache.delete(CURATED_DOMAINS_CACHE_KEY)
    yield
    cache.delete(CURATED_DOMAINS_CACHE_KEY)


def make_submission(user, name, status, **kwargs):
    return DomainSubmission.objects.create(
        name=name, submitted_by=user, status=status, **kwargs)


@pytest.mark.django_db
def test_repair_rewrites_actions_to_statuses(user, broken_column):
    approved = make_submission(user, "pudding.cool", "APPROVE")
    rejected = make_submission(user, "spam.example", "REJECT", rejection_reason="SPAM")

    repair_statuses(django_apps, None)

    approved.refresh_from_db()
    rejected.refresh_from_db()
    assert approved.status == "APPROVED"
    assert rejected.status == "REJECTED"
    # The reason the moderator chose is untouched: only the status was ever wrong.
    assert rejected.rejection_reason == "SPAM"


@pytest.mark.django_db
def test_repair_leaves_correct_rows_alone(user):
    make_submission(user, "pending.example", "PENDING")
    make_submission(user, "good.example", "APPROVED")
    make_submission(user, "bad.example", "REJECTED", rejection_reason="SPAM")

    repair_statuses(django_apps, None)

    assert DomainSubmission.objects.get(name="pending.example").status == "PENDING"
    assert DomainSubmission.objects.get(name="good.example").status == "APPROVED"
    assert DomainSubmission.objects.get(name="bad.example").status == "REJECTED"


@pytest.mark.django_db
@override_settings(HAS_DATABASE=True)
def test_repaired_approvals_reach_the_curated_domains(user, broken_column):
    """The symptom that made this visible: an approved domain is never un-blacklisted."""
    make_submission(user, "pudding.cool", "APPROVE")
    assert get_curated_domains() == set()
    cache.delete(CURATED_DOMAINS_CACHE_KEY)

    repair_statuses(django_apps, None)

    assert get_curated_domains() == {"pudding.cool"}


@pytest.mark.django_db
def test_the_database_refuses_an_action_as_a_status(user):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_submission(user, "pudding.cool", "APPROVE")


@pytest.mark.django_db
def test_the_database_refuses_an_action_as_a_status_on_update(user):
    """.update() bypasses save() and every validator above it, so the constraint is the only
    thing between a bulk write and the column."""
    make_submission(user, "pudding.cool", "PENDING")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DomainSubmission.objects.filter(name="pudding.cool").update(status="APPROVE")


@pytest.mark.django_db
def test_the_database_refuses_an_unknown_rejection_reason(user):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_submission(user, "spam.example", "REJECTED", rejection_reason="JUNK")
