"""
Reporting a search result as broken, and the background check that acts on it a day later.

A report alone changes nothing. check_reported_pages re-fetches a report's URL once it is a
day old: still gone (404/410) blocks it, answering re-indexes it, and anything inconclusive
(timeouts, robots refusals, other HTTP statuses) is left for a later check.
"""

import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from mwmbl import background
from mwmbl.background import check_reported_pages
from mwmbl.models import BlockedUrl, BrokenPageReport
from mwmbl.tinysearchengine.indexer import Document, TinyIndex

User = get_user_model()


@pytest.fixture
def verified_user():
    user = User.objects.create_user(username="reporter", email="reporter@example.com", password="testpass123")
    EmailAddress.objects.create(user=user, email="reporter@example.com", verified=True, primary=True)
    return user


@pytest.fixture
def access_token(verified_user):
    refresh = RefreshToken.for_user(verified_user)
    return str(refresh.access_token)


@pytest.mark.django_db
def test_report_broken_search_result_stores_the_report(client, verified_user, access_token):
    response = client.post(
        "/api/v1/platform/search-results/broken",
        data=json.dumps({"url": "https://example.com/dead-page", "query": "some query"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 200
    report = BrokenPageReport.objects.get(user=verified_user, url="https://example.com/dead-page")
    assert report.query == "some query"
    assert report.checked_at is None


@pytest.mark.django_db
def test_report_broken_search_result_is_idempotent(client, verified_user, access_token):
    data = {"url": "https://example.com/dead-page", "query": "some query"}
    for _ in range(2):
        response = client.post(
            "/api/v1/platform/search-results/broken",
            data=json.dumps(data),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        assert response.status_code == 200

    assert BrokenPageReport.objects.filter(user=verified_user, url=data["url"], query=data["query"]).count() == 1


@pytest.fixture
def temp_index_path():
    with TemporaryDirectory() as temp_dir:
        index_path = str(Path(temp_dir) / "temp-index.tinysearch")
        with TinyIndex.create(Document, index_path, num_pages=64, page_size=4096):
            pass
        yield index_path


def _crawl_result(url, status, title=None, extract=""):
    return {
        "url": url,
        "status": status,
        "timestamp": 1_700_000_000_000,
        "content": {"title": title, "extract": extract, "links": [], "extra_links": []} if title is not None else None,
        "error": None,
    }


@pytest.mark.django_db
def test_check_reported_pages_ignores_reports_younger_than_a_day(monkeypatch, settings, verified_user, temp_index_path):
    monkeypatch.setattr(background, "crawl_url", lambda url, redis: pytest.fail("should not crawl a fresh report"))
    settings.DATA_PATH = str(Path(temp_index_path).parent)
    settings.INDEX_NAME = Path(temp_index_path).name

    BrokenPageReport.objects.create(user=verified_user, url="https://example.com/fresh", query="q")

    check_reported_pages.now()


@pytest.mark.django_db
def test_check_reported_pages_blocks_a_url_still_gone_after_a_day(
    monkeypatch, settings, verified_user, temp_index_path
):
    url = "https://example.com/still-dead"
    report = BrokenPageReport.objects.create(user=verified_user, url=url, query="q")
    BrokenPageReport.objects.filter(pk=report.pk).update(created_at=timezone.now() - timedelta(hours=25))

    monkeypatch.setattr(background, "crawl_url", lambda crawled_url, redis: _crawl_result(crawled_url, 404))
    settings.DATA_PATH = str(Path(temp_index_path).parent)
    settings.INDEX_NAME = Path(temp_index_path).name

    check_reported_pages.now()

    assert BlockedUrl.objects.filter(url=url).exists()
    report.refresh_from_db()
    assert report.checked_at is not None


@pytest.mark.django_db
def test_check_reported_pages_reindexes_a_url_that_now_answers(monkeypatch, settings, verified_user, temp_index_path):
    url = "https://example.com/fixed"
    report = BrokenPageReport.objects.create(user=verified_user, url=url, query="q")
    BrokenPageReport.objects.filter(pk=report.pk).update(created_at=timezone.now() - timedelta(hours=25))

    monkeypatch.setattr(
        background,
        "crawl_url",
        lambda crawled_url, redis: _crawl_result(crawled_url, 200, title="Fixed page", extract="it works now"),
    )
    settings.DATA_PATH = str(Path(temp_index_path).parent)
    settings.INDEX_NAME = Path(temp_index_path).name

    check_reported_pages.now()

    assert not BlockedUrl.objects.filter(url=url).exists()
    with TinyIndex(Document, temp_index_path, "r") as indexer:
        urls = {d.url for d in indexer.retrieve("fixed")}
    assert url in urls
    report.refresh_from_db()
    assert report.checked_at is not None


@pytest.mark.django_db
def test_check_reported_pages_leaves_an_inconclusive_result_for_next_time(
    monkeypatch, settings, verified_user, temp_index_path
):
    url = "https://example.com/flaky"
    report = BrokenPageReport.objects.create(user=verified_user, url=url, query="q")
    BrokenPageReport.objects.filter(pk=report.pk).update(created_at=timezone.now() - timedelta(hours=25))

    monkeypatch.setattr(background, "crawl_url", lambda crawled_url, redis: _crawl_result(crawled_url, 503))
    settings.DATA_PATH = str(Path(temp_index_path).parent)
    settings.INDEX_NAME = Path(temp_index_path).name

    check_reported_pages.now()

    assert not BlockedUrl.objects.filter(url=url).exists()
    report.refresh_from_db()
    assert report.checked_at is not None
