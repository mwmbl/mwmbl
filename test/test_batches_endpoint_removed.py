"""
Tests for the removal of the legacy crawl-batch submission endpoint.

`POST /crawler/batches/` served the old crawler and now returns 410 Gone. Device
registration used to hang off it, so it has moved to `POST /crawler/results` - the
only remaining submission path, and the one the devices page is built on.
"""

from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client

from mwmbl.devices import HOSTNAME_MAX_LENGTH, MAX_DEVICES_PER_USER, record_device
from mwmbl.models import ApiKey, Device, generate_api_key

User = get_user_model()


@pytest.fixture
def api_client():
    return Client()


@pytest.fixture
def crawl_user(db):
    user = User.objects.create_user(
        username="crawluser",
        email="crawl@example.com",
        password="testpass123",
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


@pytest.fixture
def crawl_api_key(crawl_user):
    raw_key, key_hash = generate_api_key()
    obj = ApiKey.objects.create(
        user=crawl_user,
        key=key_hash,
        name="Test crawl key",
        scopes=[ApiKey.Scope.CRAWL],
    )
    obj.raw_key = raw_key
    return obj


def api_key_header(raw_key):
    return {"HTTP_X_API_KEY": raw_key}


@pytest.mark.django_db
def test_post_batch_returns_410(api_client):
    response = api_client.post(
        "/api/v1/crawler/batches/",
        content_type="application/json",
        data={"user_id": "a" * 64, "items": [], "device_name": "my-device"},
    )
    assert response.status_code == 410


@pytest.mark.django_db
def test_post_batch_returns_410_for_a_body_it_can_no_longer_parse(api_client):
    """The endpoint no longer binds the legacy Batch schema, so an old or malformed
    payload must still get 410 rather than a validation error."""
    response = api_client.post(
        "/api/v1/crawler/batches/",
        content_type="application/json",
        data={"nonsense": True},
    )
    assert response.status_code == 410


@pytest.mark.django_db
def test_post_results_registers_the_device(api_client, crawl_api_key, crawl_user):
    """Device registration moved here from the removed /batches/ endpoint."""
    with (
        patch("mwmbl.crawler.app.index_documents"),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager"),
    ):
        response = api_client.post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": [], "device_name": "my-device"},
            **api_key_header(crawl_api_key.raw_key),
        )

    assert response.status_code == 200
    assert Device.objects.filter(user=crawl_user, hostname="my-device").exists()


@pytest.mark.django_db
def test_post_results_without_a_device_name_registers_nothing(api_client, crawl_api_key, crawl_user):
    with (
        patch("mwmbl.crawler.app.index_documents"),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager"),
    ):
        response = api_client.post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": []},
            **api_key_header(crawl_api_key.raw_key),
        )

    assert response.status_code == 200
    assert not Device.objects.filter(user=crawl_user).exists()


@pytest.mark.django_db
def test_post_results_is_idempotent_for_a_repeated_device(api_client, crawl_api_key, crawl_user):
    """unique_together on (user, hostname) means a second submission must update, not duplicate."""
    with (
        patch("mwmbl.crawler.app.index_documents"),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager"),
    ):
        for _ in range(2):
            response = api_client.post(
                "/api/v1/crawler/results",
                content_type="application/json",
                data={"results": [], "device_name": "my-device"},
                **api_key_header(crawl_api_key.raw_key),
            )
            assert response.status_code == 200

    assert Device.objects.filter(user=crawl_user, hostname="my-device").count() == 1


@pytest.mark.django_db
def test_latest_batch_returns_410(api_client):
    """It only ever returned what POST /batches/ stored in memory."""
    response = api_client.get("/api/v1/crawler/latest-batch")
    assert response.status_code == 410


@pytest.mark.django_db
def test_post_dataset_bad_user_id_returns_400_on_the_router_mounted_api(api_client):
    """This path used to call r.create_response, which a Router does not have, so a
    wrong-length user ID raised AttributeError and 500 instead of returning 400."""
    response = api_client.post(
        "/api/v1/crawler/dataset",
        content_type="application/json",
        data={
            "user_id": "too-short",
            "date": "2026-01-01",
            "timestamp": 1704672000000,
            "extensionVersion": "0.6.1",
            "queryDataset": [],
            "searchResults": [],
        },
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_request_new_batch_returns_410(api_client):
    """It popped URLs off the queue permanently for clients that can no longer submit them."""
    response = api_client.post(
        "/api/v1/crawler/batches/new",
        content_type="application/json",
        data={"user_id": "a" * 64},
    )
    assert response.status_code == 410


@pytest.mark.django_db
def test_an_over_long_device_name_is_truncated_not_fatal(api_client, crawl_api_key, crawl_user):
    """Device.hostname is varchar(255) and Django does not validate lengths on save(), so
    an unbounded name used to raise DataError before indexing and lose the whole submission."""
    with (
        patch("mwmbl.crawler.app.index_documents") as index_documents,
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager"),
    ):
        response = api_client.post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": [], "device_name": "x" * 500},
            **api_key_header(crawl_api_key.raw_key),
        )

    assert response.status_code == 200
    index_documents.assert_called_once()
    assert Device.objects.get(user=crawl_user).hostname == "x" * HOSTNAME_MAX_LENGTH


@pytest.mark.django_db
def test_devices_per_user_are_capped(crawl_user):
    """A client varying the name it reports must not grow the table without bound."""
    for i in range(MAX_DEVICES_PER_USER + 10):
        record_device(crawl_user, f"device-{i}")

    assert Device.objects.filter(user=crawl_user).count() == MAX_DEVICES_PER_USER
    # The most recent survive; the least recently seen are the ones dropped.
    assert Device.objects.filter(user=crawl_user, hostname=f"device-{MAX_DEVICES_PER_USER + 9}").exists()


@pytest.mark.django_db
def test_the_api_key_is_stripped_from_the_uploaded_object(api_client, crawl_api_key, crawl_user):
    """The results bucket is world-readable, so a key passed in the deprecated body field
    must not be uploaded with the rest of the submission."""
    with (
        patch("mwmbl.crawler.app.index_documents"),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz") as upload_object,
        patch("mwmbl.crawler.app.stats_manager"),
    ):
        response = api_client.post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": [], "api_key": crawl_api_key.raw_key},
        )

    assert response.status_code == 200
    uploaded = upload_object.call_args.args[0]
    assert uploaded.api_key is None
