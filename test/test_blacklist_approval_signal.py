"""Tests for the snapshot rebuild scheduled when a domain submission is approved.

Approved domains are subtracted from the remote blocklists when the snapshot is built, so
without this an approval would sit inert until the next periodic rebuild - six hours by
default. The scheduling is debounced because moderators work through the queue in batches.
"""
from datetime import timedelta

import pytest
from background_task.models import Task
from django.test import override_settings
from django.utils import timezone

from mwmbl.models import DomainSubmission, MwmblUser
from mwmbl.signals import BLACKLIST_SNAPSHOT_TASK

APPROVAL_DELAY = 600


@pytest.fixture
def user(db):
    return MwmblUser.objects.create(username="curator")


def snapshot_tasks():
    return Task.objects.filter(task_name=BLACKLIST_SNAPSHOT_TASK)


@pytest.mark.django_db
@override_settings(BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS=APPROVAL_DELAY)
def test_approving_a_submission_schedules_a_rebuild(user):
    before = timezone.now()
    DomainSubmission.objects.create(name="pudding.cool", status="APPROVED", submitted_by=user)

    task = snapshot_tasks().get()
    assert task.run_at >= before + timedelta(seconds=APPROVAL_DELAY - 5)
    assert task.repeat == 0


@pytest.mark.django_db
@override_settings(BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS=APPROVAL_DELAY)
def test_a_batch_of_approvals_schedules_one_rebuild(user):
    for i in range(10):
        DomainSubmission.objects.create(name=f"site{i}.example", status="APPROVED", submitted_by=user)

    assert snapshot_tasks().count() == 1


@pytest.mark.django_db
@override_settings(BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS=APPROVAL_DELAY)
def test_pending_and_rejected_submissions_schedule_nothing(user):
    DomainSubmission.objects.create(name="pending.example", status="PENDING", submitted_by=user)
    DomainSubmission.objects.create(name="rejected.example", status="REJECTED", submitted_by=user)

    assert snapshot_tasks().count() == 0


@pytest.mark.django_db
@override_settings(BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS=APPROVAL_DELAY)
def test_a_rebuild_already_due_inside_the_window_suppresses_a_new_one(user):
    """The periodic task's own row counts, so an approval shortly before the six-hourly
    rebuild adds nothing at all."""
    from mwmbl.background import refresh_blacklist_snapshot
    refresh_blacklist_snapshot(repeat=6 * 60 * 60, repeat_until=None)
    assert snapshot_tasks().count() == 1

    DomainSubmission.objects.create(name="pudding.cool", status="APPROVED", submitted_by=user)

    assert snapshot_tasks().count() == 1


@pytest.mark.django_db
@override_settings(BLACKLIST_SNAPSHOT_APPROVAL_DELAY_SECONDS=APPROVAL_DELAY)
def test_a_rebuild_due_after_the_window_does_not_suppress_a_new_one(user):
    from mwmbl.background import refresh_blacklist_snapshot
    refresh_blacklist_snapshot(schedule=APPROVAL_DELAY * 10)
    assert snapshot_tasks().count() == 1

    DomainSubmission.objects.create(name="pudding.cool", status="APPROVED", submitted_by=user)

    assert snapshot_tasks().count() == 2
