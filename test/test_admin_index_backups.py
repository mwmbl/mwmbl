"""Tests for the index backups admin page (mwmbl.admin_views.index_backups_view)."""

from datetime import datetime, timedelta, timezone

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model

from mwmbl.models import IndexBackup

User = get_user_model()

STATUS_URL = "/admin/index-backups/"
SIZE = 400 * 1024**3


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user(username="staff_member_1", password="correctpassword", is_staff=True)
    client.force_login(user)
    return client


def _backup(status, started, finished=None, bytes_read=0, error=None, updated_at=None):
    backup = IndexBackup.objects.create(
        index_name=settings.INDEX_NAME,
        key=f"index/{settings.INDEX_NAME}/full/{started:%Y-%m-%dT%H%M%SZ}.zst",
        status=status,
        size=SIZE,
        bytes_read=bytes_read,
        error=error,
        started=started,
        finished=finished,
    )
    if updated_at is not None:
        # auto_now sets it on every save, so only update() can put it in the past.
        IndexBackup.objects.filter(pk=backup.pk).update(updated_at=updated_at)
    return backup


def test_non_staff_are_redirected_to_login(client, db):
    response = client.get(STATUS_URL)

    assert response.status_code == 302
    assert "/admin/login/" in response.url


def test_no_backups_is_overdue(staff_client):
    response = staff_client.get(STATUS_URL)

    assert response.status_code == 200
    assert response.context["overdue"]
    assert b"No backup has run yet" in response.content


def test_a_recent_success_is_not_overdue(staff_client):
    now = datetime.now(timezone.utc)
    success = _backup(IndexBackup.Status.SUCCEEDED, now - timedelta(days=3, hours=4), now - timedelta(days=3), SIZE)

    response = staff_client.get(STATUS_URL)

    assert response.context["last_success"] == success
    assert not response.context["overdue"]
    assert success.key.encode() in response.content


def test_an_old_success_is_overdue_even_with_a_later_failure(staff_client):
    now = datetime.now(timezone.utc)
    _backup(IndexBackup.Status.SUCCEEDED, now - timedelta(days=40), now - timedelta(days=40), SIZE)
    _backup(IndexBackup.Status.FAILED, now - timedelta(days=9), now - timedelta(days=9), error="NoSuchBucket: gone")

    response = staff_client.get(STATUS_URL)

    assert response.context["overdue"]
    assert b"NoSuchBucket: gone" in response.content


def test_a_running_backup_shows_its_progress_and_stalls_without_updates(staff_client):
    now = datetime.now(timezone.utc)
    _backup(IndexBackup.Status.RUNNING, now - timedelta(minutes=30), bytes_read=SIZE // 4)
    _backup(
        IndexBackup.Status.RUNNING, now - timedelta(days=2), bytes_read=SIZE // 2, updated_at=now - timedelta(days=2)
    )

    response = staff_client.get(STATUS_URL)

    running, stalled = response.context["backups"]
    assert running["percent_done"] == 25
    assert not running["stalled"]
    assert stalled["stalled"]
    assert b"stalled" in response.content
