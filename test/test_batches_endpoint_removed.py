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
