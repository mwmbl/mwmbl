"""
The crawler API's read endpoints and dataset upload: the S3 layout they share, and
the input checks that stop a request reaching the bucket.
"""

import gzip
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from django.test import Client

from mwmbl.crawler import app
from mwmbl.crawler.batch import HashedBatch
from mwmbl.settings import PUBLIC_URL_PREFIX

USER_HASH = "a" * 64
DATE = "2024-01-02"


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def s3(monkeypatch):
    boto3 = MagicMock()
    monkeypatch.setattr(app, "boto3", boto3)
    return boto3


def test_upload_object_names_the_file_after_date_type_user_and_time(s3):
    now = datetime(2024, 1, 2, 0, 1, 5, tzinfo=timezone.utc)
    batch = HashedBatch(user_id_hash=USER_HASH, timestamp=1.0, items=[])

    filename = app.upload_object(batch, now, USER_HASH, "results")

    prefix = f"1/{app.VERSION}/{DATE}/results/{USER_HASH}/00065__"
    assert filename.startswith(prefix)
    assert filename.endswith(".json.gz")
    body = s3.resource.return_value.Object.return_value.put.call_args.kwargs["Body"]
    assert json.loads(gzip.decompress(body))["user_id_hash"] == USER_HASH


def test_batch_ids_are_the_sorted_file_stems(s3):
    keys = [f"1/{app.VERSION}/{DATE}/1/{USER_HASH}/{name}.json.gz" for name in ("b", "a")]
    s3.resource.return_value.Bucket.return_value.objects.filter.return_value = [MagicMock(key=k) for k in keys]

    result = app.get_batch_ids_for_prefix("some/prefix/")

    assert result == {"batch_ids": ["a", "b"]}


def test_subfolders_are_the_user_hashes_under_a_date(s3):
    prefix = f"1/{app.VERSION}/{DATE}/1/"
    s3.client.return_value.list_objects.return_value = {"CommonPrefixes": [{"Prefix": f"{prefix}{USER_HASH}/"}]}

    assert app.get_subfolders(prefix) == [USER_HASH]


def test_listing_batches_for_a_user_and_date(client, s3):
    key = f"1/{app.VERSION}/{DATE}/1/{USER_HASH}/batch-1.json.gz"
    s3.resource.return_value.Bucket.return_value.objects.filter.return_value = [MagicMock(key=key)]

    response = client.get(f"/crawler/batches/{DATE}/users/{USER_HASH}")

    assert response.status_code == 200
    assert response.json() == {"batch_ids": ["batch-1"]}


def test_listing_crawlers_active_on_a_date(client, s3):
    prefix = f"1/{app.VERSION}/{DATE}/1/"
    s3.client.return_value.list_objects.return_value = {"CommonPrefixes": [{"Prefix": f"{prefix}{USER_HASH}/"}]}

    response = client.get(f"/crawler/batches/{DATE}/users")

    assert response.status_code == 200
    assert response.json() == [USER_HASH]


@pytest.mark.parametrize(
    "path",
    [
        f"/crawler/batches/20240102/users/{USER_HASH}",
        f"/crawler/batches/{DATE}/users/short",
        "/crawler/batches/yesterday/users",
    ],
)
def test_malformed_dates_and_user_ids_are_rejected_before_touching_s3(client, s3, path):
    response = client.get(path)

    assert response.status_code == 400
    s3.resource.assert_not_called()
    s3.client.assert_not_called()


def test_fetching_a_batch_returns_its_public_url_and_content(client, monkeypatch):
    batch = {"user_id_hash": USER_HASH, "items": []}
    fetched = []

    def fake_get(url):
        fetched.append(url)
        return MagicMock(content=gzip.compress(json.dumps(batch).encode()))

    monkeypatch.setattr(app.requests, "get", fake_get)

    response = client.get(f"/crawler/batches/{DATE}/users/{USER_HASH}/batch/batch-1")

    expected_url = f"{PUBLIC_URL_PREFIX}1/{app.VERSION}/{DATE}/1/{USER_HASH}/batch-1.json.gz"
    assert response.status_code == 200
    assert response.json() == {"url": expected_url, "batch": batch}
    assert fetched == [expected_url]


def test_latest_batch_is_gone(client):
    assert client.get("/crawler/latest-batch").status_code == 410


def test_crawler_status_is_ok(client):
    assert client.get("/crawler/").json() == {"status": "ok"}


def _dataset(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "date": DATE,
        "timestamp": 1704672000000,
        "extensionVersion": "0.6.1",
        "queryDataset": [],
        "searchResults": [],
    }


def test_dataset_with_a_wrong_length_user_id_is_rejected(client, s3):
    response = client.post("/crawler/dataset", data=_dataset("short"), content_type="application/json")

    assert response.status_code == 400
    s3.resource.assert_not_called()


def test_dataset_is_stored_under_the_hash_of_the_user_id(client, s3, monkeypatch):
    stats = MagicMock()
    monkeypatch.setattr(app, "stats_manager", stats)
    user_id = "b" * app.USER_ID_LENGTH

    response = client.post("/crawler/dataset", data=_dataset(user_id), content_type="application/json")

    body = response.json()
    assert response.status_code == 200
    assert body["public_user_id"] == app._get_user_id_hash(MagicMock(user_id=user_id))
    assert user_id not in body["url"]
    stats.record_dataset.assert_called_once()


def test_curated_domains_endpoint_lists_domains_sorted(client, monkeypatch):
    monkeypatch.setattr("mwmbl.curated_domains.get_curated_domains", lambda: {"b.example", "a.example"})

    response = client.get("/crawler/curated-domains")

    assert response.json() == {"domains": [{"name": "a.example"}, {"name": "b.example"}]}
