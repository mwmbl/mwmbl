"""
What each of a user's devices has submitted: a lifetime total, the last hour and day, and each of the
last 30 days - and removing a device from the list without losing that.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl.devices import (
    MAX_DEVICES_PER_USER,
    SUBMISSION_HISTORY_DAYS,
    get_device_activity,
    record_device,
    record_device_results,
)
from mwmbl.models import ApiKey, Device, DeviceSubmission, generate_api_key

User = get_user_model()

BASE = "/api/v1/platform"
NOW = datetime(2026, 11, 2, 12, 30, tzinfo=timezone.utc)


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {RefreshToken.for_user(user).access_token}"}


@pytest.fixture
def user(db):
    user = User.objects.create_user(username="heron", email="heron@example.com", password="correctpassword")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user


@pytest.fixture
def device(user):
    return Device.objects.create(user=user, hostname="blue-heron-1")


def test_results_are_counted_for_the_lifetime_total_and_each_window(device):
    record_device_results(device, 10, NOW - timedelta(days=2))
    record_device_results(device, 20, NOW - timedelta(hours=5))
    record_device_results(device, 30, NOW - timedelta(minutes=10))

    activity = get_device_activity([device.id], NOW)[device.id]

    device.refresh_from_db()
    assert device.total_results == 60
    assert activity["results_last_hour"] == 30
    assert activity["results_last_24h"] == 50


def test_results_daily_covers_30_days_oldest_first_with_empty_days_included(device):
    record_device_results(device, 7, NOW - timedelta(days=2))
    record_device_results(device, 5, NOW - timedelta(days=2, hours=1))

    daily = get_device_activity([device.id], NOW)[device.id]["results_daily"]

    assert len(daily) == SUBMISSION_HISTORY_DAYS
    assert daily[-1]["date"] == NOW.date()
    assert [(d["date"], d["results"]) for d in daily if d["results"]] == [(NOW.date() - timedelta(days=2), 12)]


def test_a_device_with_no_submissions_has_zero_activity(device):
    activity = get_device_activity([device.id], NOW)[device.id]

    assert activity["results_last_hour"] == 0
    assert activity["results_last_24h"] == 0
    assert all(day["results"] == 0 for day in activity["results_daily"])


def test_submissions_older_than_the_history_are_dropped_but_still_in_the_total(device):
    record_device_results(device, 100, NOW - timedelta(days=SUBMISSION_HISTORY_DAYS + 1))
    record_device_results(device, 1, NOW)

    assert DeviceSubmission.objects.filter(device=device).count() == 1
    device.refresh_from_db()
    assert device.total_results == 101


def test_windows_include_their_start_and_days_split_at_utc_midnight(device):
    midnight = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
    record_device_results(device, 1, midnight - timedelta(seconds=1))
    record_device_results(device, 2, midnight)
    record_device_results(device, 4, NOW - timedelta(hours=1))
    record_device_results(device, 8, NOW - timedelta(hours=24))
    record_device_results(device, 16, NOW - timedelta(hours=24, seconds=1))

    activity = get_device_activity([device.id], NOW)[device.id]

    assert activity["results_last_hour"] == 4
    assert activity["results_last_24h"] == 1 + 2 + 4 + 8
    by_day = {d["date"]: d["results"] for d in activity["results_daily"]}
    assert by_day[NOW.date()] == 2 + 4
    assert by_day[NOW.date() - timedelta(days=1)] == 1 + 8 + 16


def test_an_empty_submission_leaves_no_row(device):
    record_device_results(device, 0, NOW)

    assert not DeviceSubmission.objects.filter(device=device).exists()


def test_hidden_devices_are_not_dropped_when_over_the_device_cap(user, device):
    Device.objects.filter(pk=device.pk).update(hidden=True, total_results=50)
    for i in range(MAX_DEVICES_PER_USER + 1):
        record_device(user, f"newer-{i}")

    device.refresh_from_db()
    assert device.total_results == 50
    assert Device.objects.filter(user=user, hidden=False).count() == MAX_DEVICES_PER_USER


def test_the_devices_endpoints_need_a_verified_email(user, device):
    EmailAddress.objects.filter(user=user).update(verified=False)

    activity = Client().get(f"{BASE}/devices/activity/", headers=_auth(user))
    removal = Client().delete(f"{BASE}/devices/{device.id}", headers=_auth(user))

    assert activity.status_code == 403
    assert removal.status_code == 403


def test_a_device_already_removed_cannot_be_removed_again(user, device):
    first = Client().delete(f"{BASE}/devices/{device.id}", headers=_auth(user))
    second = Client().delete(f"{BASE}/devices/{device.id}", headers=_auth(user))

    assert (first.status_code, second.status_code) == (200, 404)


def test_devices_do_not_share_counts(user, device):
    other = Device.objects.create(user=user, hostname="blue-heron-2")
    record_device_results(device, 4, NOW)

    activity = get_device_activity([device.id, other.id], NOW)

    assert activity[device.id]["results_last_24h"] == 4
    assert activity[other.id]["results_last_24h"] == 0


def test_the_activity_endpoint_reports_the_counts(user, device):
    record_device_results(device, 8, datetime.now(timezone.utc))

    response = Client().get(f"{BASE}/devices/activity/", headers=_auth(user))

    [body] = response.json()
    assert body["device_id"] == device.id
    assert body["results_total"] == 8
    assert body["results_last_hour"] == 8
    assert body["results_last_24h"] == 8
    assert len(body["results_daily"]) == SUBMISSION_HISTORY_DAYS


def test_the_activity_endpoint_covers_only_the_callers_listed_devices(user, device):
    stranger = User.objects.create_user(username="other", email="other@example.com", password="correctpassword")
    EmailAddress.objects.create(user=stranger, email=stranger.email, verified=True, primary=True)
    Device.objects.create(user=stranger, hostname="not-mine")
    Device.objects.create(user=user, hostname="gone", hidden=True)

    response = Client().get(f"{BASE}/devices/activity/", headers=_auth(user))

    assert [d["device_id"] for d in response.json()] == [device.id]


def test_a_removed_device_is_hidden_but_keeps_its_counts(user, device):
    record_device_results(device, 8, NOW)

    response = Client().delete(f"{BASE}/devices/{device.id}", headers=_auth(user))

    assert response.status_code == 200
    assert Client().get(f"{BASE}/devices/", headers=_auth(user)).json() == []
    device.refresh_from_db()
    assert device.total_results == 8
    assert DeviceSubmission.objects.filter(device=device).count() == 1


def test_a_device_can_be_removed_by_its_owner_only(user, device):
    stranger = User.objects.create_user(username="other", email="other@example.com", password="correctpassword")
    EmailAddress.objects.create(user=stranger, email=stranger.email, verified=True, primary=True)

    refused = Client().delete(f"{BASE}/devices/{device.id}", headers=_auth(stranger))

    assert refused.status_code == 404
    device.refresh_from_db()
    assert not device.hidden


def test_a_removed_device_reappears_when_its_crawler_submits_again(user, device):
    Device.objects.filter(pk=device.pk).update(hidden=True)

    record_device(user, device.hostname)

    device.refresh_from_db()
    assert not device.hidden


@pytest.mark.django_db
def test_post_results_counts_the_results_against_the_device(user):
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=user, key=key_hash, name="Crawl", scopes=[ApiKey.Scope.CRAWL])
    results = [{"url": f"https://example.com/{i}", "title": "t", "extract": "e", "links": []} for i in range(3)]

    with (
        patch("mwmbl.crawler.app.index_documents"),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager"),
        patch("mwmbl.crawler.app.record_agreements"),
        patch("mwmbl.crawler.app.worst_agreements"),
    ):
        response = Client().post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": results, "device_name": "blue-heron-1", "crawler_version": "1.0.5"},
            HTTP_X_API_KEY=raw_key,
        )

    assert response.status_code == 200
    device = Device.objects.get(user=user, hostname="blue-heron-1")
    assert device.total_results == 3
    assert device.crawler_version == "1.0.5"


def test_a_submission_records_the_crawlers_version_and_a_later_one_updates_it(user):
    record_device(user, "versioned", "1.0.4")
    record_device(user, "versioned", "1.0.5")
    record_device(user, "versioned")

    device = Device.objects.get(user=user, hostname="versioned")
    assert device.crawler_version == "1.0.5"
    [body] = Client().get(f"{BASE}/devices/", headers=_auth(user)).json()
    assert body["crawler_version"] == "1.0.5"


def test_an_over_long_version_is_truncated_rather_than_losing_the_submission(user):
    record_device(user, "long-version", "9" * 1000)

    assert len(Device.objects.get(user=user, hostname="long-version").crawler_version) == 255
