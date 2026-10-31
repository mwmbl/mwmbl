"""Tests for MIN_CRAWLER_VERSION: the results API rejects too-old crawlers with 426."""

from unittest.mock import patch

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from mwmbl.crawler.stats import StatsManager
from mwmbl.crawler.version import parse_release_version
from mwmbl.models import ApiKey, generate_api_key

User = get_user_model()

RESULTS_URL = "/api/v1/crawler/results"


@pytest.mark.parametrize(
    "version, expected",
    [
        ("1.2.3", (1, 2, 3)),
        ("0.10.0", (0, 10, 0)),
        ("01.2.3", (1, 2, 3)),
        ("", None),
        ("1.2", None),
        ("1.2.3.4", None),
        ("latest", None),
        ("1.2.x", None),
        ("1.².3", None),
        ("1.٣.0", None),
        ("1." + "9" * 7 + ".0", None),
    ],
)
def test_parse_release_version_accepts_only_ascii_x_y_z(version, expected):
    assert parse_release_version(version) == expected


@pytest.fixture
def stats_manager():
    return StatsManager(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="test-password")


@pytest.fixture
def crawl_key(alice):
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=alice, key=key_hash, name="Crawl key", scopes=[ApiKey.Scope.CRAWL])
    return raw_key


def _post_results(client, stats_manager, crawl_key, version="0.2.0", dry_run=False):
    path = RESULTS_URL + ("?dry_run=true" if dry_run else "")
    payload = {
        "results": [{"url": "https://example.com/a", "title": "t", "extract": "e"}],
        "device_name": "crawler-aaaa",
    }
    if version is not None:
        payload["crawler_version"] = version
    with (
        patch("mwmbl.crawler.app.index_documents") as index_documents,
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager", stats_manager),
        patch("mwmbl.crawler.app.record_agreements"),
        patch("mwmbl.crawler.worst_agreements.record_worst_agreements"),
    ):
        response = client.post(path, content_type="application/json", data=payload, HTTP_X_API_KEY=crawl_key)
    return response, index_documents


def test_a_crawler_below_the_minimum_version_gets_426(client, stats_manager, crawl_key):
    with override_settings(MIN_CRAWLER_VERSION="9.9.9"):
        response, index_documents = _post_results(client, stats_manager, crawl_key, version="0.2.0")

    assert response.status_code == 426
    message = response.json()["message"]
    assert "0.2.0" in message
    assert "9.9.9" in message
    index_documents.assert_not_called()
    assert stats_manager.get_crawler_version_spread(7).rows == []


def test_versions_are_compared_as_numbers_not_strings(client, stats_manager, crawl_key):
    # "0.10.0" sorts after "0.9.0" as a tuple but before it as a string.
    with override_settings(MIN_CRAWLER_VERSION="0.9.0"):
        response, _ = _post_results(client, stats_manager, crawl_key, version="0.10.0")
    assert response.status_code == 200

    with override_settings(MIN_CRAWLER_VERSION="0.10.0"):
        response, _ = _post_results(client, stats_manager, crawl_key, version="0.9.0")
    assert response.status_code == 426


def test_a_crawler_at_the_minimum_version_is_accepted(client, stats_manager, crawl_key):
    with override_settings(MIN_CRAWLER_VERSION="0.2.0"):
        response, _ = _post_results(client, stats_manager, crawl_key, version="0.2.0")
    assert response.status_code == 200


def test_a_crawler_with_no_version_passes_while_the_minimum_is_zero(client, stats_manager, crawl_key):
    response, _ = _post_results(client, stats_manager, crawl_key, version=None)
    assert response.status_code == 200

    with override_settings(MIN_CRAWLER_VERSION="1.0.0"):
        response, index_documents = _post_results(client, stats_manager, crawl_key, version=None)
    assert response.status_code == 426
    assert "unknown" in response.json()["message"]
    index_documents.assert_not_called()


def test_a_malformed_version_counts_as_too_old_once_the_minimum_is_raised(client, stats_manager, crawl_key):
    response, _ = _post_results(client, stats_manager, crawl_key, version="latest")
    assert response.status_code == 200

    with override_settings(MIN_CRAWLER_VERSION="0.0.1"):
        response, _ = _post_results(client, stats_manager, crawl_key, version="latest")
    assert response.status_code == 426


def test_a_dry_run_from_an_old_crawler_is_rejected_too(client, stats_manager, crawl_key):
    with override_settings(MIN_CRAWLER_VERSION="9.9.9"):
        response, index_documents = _post_results(client, stats_manager, crawl_key, version="0.2.0", dry_run=True)

    assert response.status_code == 426
    index_documents.assert_not_called()


def test_an_invalid_minimum_version_setting_fails_loudly(client, stats_manager, crawl_key):
    with override_settings(MIN_CRAWLER_VERSION="bogus"), pytest.raises(AssertionError):
        _post_results(client, stats_manager, crawl_key)
