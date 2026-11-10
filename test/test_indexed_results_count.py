"""
POST /crawler/results counts the URLs a submission added to the index, not what it sent: a
crawler resubmits what the index did not keep and repeats a URL once per term it holds it
under, so counting the submission counted the same pages over and over (#608).
"""

from unittest.mock import patch

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from mwmbl.indexer.blacklist_providers import StaticBlacklistProvider
from mwmbl.indexer.blacklist_snapshot import SnapshotBlacklist
from mwmbl.indexer.index_batches import index_documents
from mwmbl.models import ApiKey, generate_api_key
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, TinyIndex

INDEX_NAME = "test.tinysearch"


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / INDEX_NAME
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=100_000, page_size=PAGE_SIZE)
    return str(path)


@pytest.fixture
def no_blacklist():
    blacklist = SnapshotBlacklist(built_in_rules=StaticBlacklistProvider(set()), redis_client=fakeredis.FakeRedis())
    with patch("mwmbl.indexer.index_batches.get_snapshot_blacklist", return_value=blacklist):
        yield


def _result(url: str, title: str) -> dict:
    return {"url": url, "title": title, "extract": f"All about {title.lower()}"}


def test_index_documents_reports_only_urls_the_index_lacked(index_path, no_blacklist):
    first = Document("Python tutorial", "https://example.com/python", "Learn python")
    second = Document("Rust tutorial", "https://example.com/rust", "Learn rust")

    assert index_documents([first, first], index_path).urls.new == {first.url}
    assert index_documents([first, second], index_path).urls.new == {second.url}


@pytest.mark.django_db
def test_post_results_counts_each_new_url_once(tmp_path, index_path, no_blacklist):
    user = get_user_model().objects.create_user(username="crawler", email="crawler@example.com", password="pw")
    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(user=user, key=key_hash, name="Crawl key", scopes=[ApiKey.Scope.CRAWL])
    python = _result("https://example.com/python", "Python tutorial")
    rust = _result("https://example.com/rust", "Rust tutorial")

    def post(results):
        response = Client().post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": results},
            HTTP_X_API_KEY=raw_key,
        )
        assert response.status_code == 200

    with (
        patch("mwmbl.crawler.app.settings.DATA_PATH", str(tmp_path)),
        patch("mwmbl.crawler.app.settings.INDEX_NAME", INDEX_NAME),
        patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"),
        patch("mwmbl.crawler.app.stats_manager") as stats_manager,
        patch("mwmbl.crawler.app.record_agreements"),
        patch("mwmbl.crawler.app.worst_agreements"),
    ):
        # The same URL once for each of two terms, as the crawler sends it.
        post([python, python])
        # Resubmitted alongside one the index has not seen.
        post([python, rust])

    counted = [call.args[2] for call in stats_manager.record_results.call_args_list]
    assert counted == [1, 1]
