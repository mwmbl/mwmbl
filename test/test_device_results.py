"""
What each of a user's devices has submitted: a lifetime total, the last hour and day, and each of the
last 30 days.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl.devices import SUBMISSION_HISTORY_DAYS, get_device_activity, record_device_results
from mwmbl.models import ApiKey, Device, DeviceSubmission, generate_api_key

User = get_user_model()

NOW = datetime(2026, 11, 2, 12, 30, tzinfo=timezone.utc)


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


def test_devices_do_not_share_counts(user, device):
    other = Device.objects.create(user=user, hostname="blue-heron-2")
    record_device_results(device, 4, NOW)

    activity = get_device_activity([device.id, other.id], NOW)

    assert activity[device.id]["results_last_24h"] == 4
    assert activity[other.id]["results_last_24h"] == 0


def test_the_devices_endpoint_reports_the_counts(user, device):
    record_device_results(device, 8, datetime.now(timezone.utc))
    headers = {"Authorization": f"Bearer {RefreshToken.for_user(user).access_token}"}

    response = Client().get("/api/v1/platform/devices/", headers=headers)

    [body] = response.json()
    assert body["results_total"] == 8
    assert body["results_last_hour"] == 8
    assert body["results_last_24h"] == 8
    assert len(body["results_daily"]) == SUBMISSION_HISTORY_DAYS


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
            data={"results": results, "device_name": "blue-heron-1"},
            HTTP_X_API_KEY=raw_key,
        )

    assert response.status_code == 200
    assert Device.objects.get(user=user, hostname="blue-heron-1").total_results == 3
