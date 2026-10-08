"""Tests for the index count admin page (mwmbl.admin_views.index_count_view)."""

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from redis import ConnectionError as RedisConnectionError

from mwmbl import count_urls
from mwmbl.count_urls import count_urls_step
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, TinyIndex

User = get_user_model()

STATUS_URL = "/admin/index-count/"
NUM_PAGES = 4


@pytest.fixture
def redis(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(count_urls, "get_redis", lambda: client)
    return client


@pytest.fixture
def index_path(tmp_path, settings):
    settings.DATA_PATH = str(tmp_path)
    path = tmp_path / settings.INDEX_NAME
    TinyIndex.create(item_factory=Document, index_path=str(path), num_pages=NUM_PAGES, page_size=PAGE_SIZE)
    with TinyIndex(Document, str(path), "w") as index:
        index.store_in_page(0, [Document(title="A", url="https://a.test/", extract="x", score=1.0, term="a")])
    return path


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user(username="staff_member_1", password="correctpassword", is_staff=True)
    client.force_login(user)
    return client


def test_non_staff_are_redirected_to_login(client, db):
    response = client.get(STATUS_URL)

    assert response.status_code == 302
    assert "/admin/login/" in response.url


def test_shows_a_scan_in_progress(staff_client, redis, index_path, monkeypatch):
    monkeypatch.setattr(count_urls, "NUM_PAGES_IN_BATCH", 1)
    count_urls_step(redis, index_path, time_budget_seconds=0)

    response = staff_client.get(STATUS_URL)

    assert response.status_code == 200
    assert response.context["scan"]["in_progress"]
    assert response.context["scan"]["next_page"] == 1
    assert b"page 1 of 4" in response.content


def test_shows_published_counts(staff_client, redis, index_path):
    count_urls_step(redis, index_path, time_budget_seconds=60)

    response = staff_client.get(STATUS_URL)

    assert response.status_code == 200
    assert not response.context["scan"]["in_progress"]
    assert response.context["published"][0]["urls"] == 1
    task_names = [task["name"] for task in response.context["tasks"]]
    assert task_names == ["mwmbl.background.count_index_urls"]


def test_renders_when_redis_is_down(staff_client, index_path, monkeypatch):
    def unreachable():
        raise RedisConnectionError("Connection refused")

    monkeypatch.setattr(count_urls, "get_redis", unreachable)

    response = staff_client.get(STATUS_URL)

    assert response.status_code == 200
    assert b"Redis unreachable" in response.content
