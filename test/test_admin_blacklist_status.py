"""Tests for the blacklist status admin page (mwmbl.admin_views)."""
import fakeredis
import numpy as np
import pytest
from django.contrib.auth import get_user_model
from redis import ConnectionError as RedisConnectionError

from mwmbl import admin_views
from mwmbl.indexer import blacklist_snapshot, purge_queue
from mwmbl.indexer.blacklist_snapshot import (
    HASH_DTYPE, SnapshotBlacklist, hash_domains, publish_snapshot,
)
from mwmbl.indexer.purge_queue import drain_purge_queue, enqueue_for_purge, peek_purge_queue, queue_size
from mwmbl.tinysearchengine.indexer import Document

User = get_user_model()

STATUS_URL = "/admin/blacklist-status/"


@pytest.fixture(autouse=True)
def clear_recently_enqueued():
    """purge_queue keeps a process-level record of what it has already queued, so without
    this each test would inherit the previous one's suppressions."""
    purge_queue._recently_enqueued.clear()
    yield
    purge_queue._recently_enqueued.clear()


@pytest.fixture
def redis_server():
    return fakeredis.FakeServer()


@pytest.fixture
def redis_client(redis_server):
    return fakeredis.FakeRedis(server=redis_server, decode_responses=True)


@pytest.fixture
def documents():
    return [
        Document(title="Bad", url="https://badsite.test/a", extract="x", score=1.0),
        Document(title="Worse", url="https://badsite.test/b", extract="y", score=2.0, term="query term"),
    ]


@pytest.fixture
def wired_redis(monkeypatch, redis_server, redis_client):
    """Point both Redis-backed modules at one fake server, and reset the snapshot singleton.

    Two clients over one server, as in production: blacklist_snapshot's is binary because
    the snapshot blob is not text, purge_queue's decodes because its payloads are JSON.
    Sharing the server is what lets a test set up a snapshot and a queue and see both on
    the same page.
    """
    binary_client = fakeredis.FakeRedis(server=redis_server)
    monkeypatch.setattr(blacklist_snapshot, "_redis", binary_client)
    monkeypatch.setattr(purge_queue, "_redis", redis_client)
    monkeypatch.setattr(blacklist_snapshot, "_snapshot_blacklist",
                        SnapshotBlacklist(redis_client=binary_client))
    return redis_client


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user(username="staff_member_1", password="correctpassword", is_staff=True)
    client.force_login(user)
    return client


# ---------------------------------------------------------------------------
# peek_purge_queue
# ---------------------------------------------------------------------------

def test_peek_does_not_consume_the_queue(redis_client, documents):
    enqueue_for_purge(documents, redis_client)

    peeked = peek_purge_queue(10, redis_client)

    assert {d.url for d in peeked} == {"https://badsite.test/a", "https://badsite.test/b"}
    assert queue_size(redis_client) == 2
    # And the entries are still there for the purge task to take.
    assert len(drain_purge_queue(10, redis_client)) == 2


def test_peek_returns_the_queued_term(redis_client, documents):
    enqueue_for_purge(documents, redis_client)

    terms = {d.url: d.term for d in peek_purge_queue(10, redis_client)}

    assert terms["https://badsite.test/b"] == "query term"
    assert terms["https://badsite.test/a"] is None


def test_peek_on_an_empty_queue(redis_client):
    assert peek_purge_queue(10, redis_client) == []


# ---------------------------------------------------------------------------
# Status gathering
# ---------------------------------------------------------------------------

def test_snapshot_status_reports_a_published_blob(wired_redis):
    blob = np.array([1, 2, 3], dtype=HASH_DTYPE).tobytes()
    version = publish_snapshot(blob, wired_redis)

    status = admin_views._snapshot_status()

    assert status["blob_present"]
    assert status["blob_size"] == 24
    assert status["blob_domains"] == 3
    assert status["blob_size_valid"]
    assert status["published_version"] == version


def test_snapshot_status_when_nothing_is_published(wired_redis):
    status = admin_views._snapshot_status()

    assert not status["blob_present"]
    assert status["blob_size"] == 0
    assert status["published_version"] is None
    assert status["loaded_version"] is None
    assert status["loaded_domains"] == 0


def test_snapshot_status_flags_a_blob_that_is_not_a_whole_number_of_hashes(wired_redis):
    publish_snapshot(b"12345", wired_redis)

    status = admin_views._snapshot_status()

    assert status["blob_present"]
    assert not status["blob_size_valid"]


def test_snapshot_status_distinguishes_published_from_loaded(wired_redis):
    publish_snapshot(np.array([1, 2], dtype=HASH_DTYPE).tobytes(), wired_redis)
    status_before_load = admin_views._snapshot_status()

    blacklist_snapshot.get_snapshot_blacklist().load_now()
    status_after_load = admin_views._snapshot_status()

    assert not status_before_load["up_to_date"]
    assert status_after_load["up_to_date"]
    assert status_after_load["loaded_domains"] == 2


def test_queue_status_samples_without_draining(wired_redis, documents):
    enqueue_for_purge(documents, wired_redis)

    status = admin_views._queue_status()

    assert status["size"] == 2
    assert len(status["sample"]) == 2
    assert not status["full"]
    assert queue_size(wired_redis) == 2


def test_removed_counts_covers_the_requested_window(wired_redis):
    counts = admin_views._removed_counts(14)

    assert len(counts) == 14
    assert all(day["count"] == 0 for day in counts)
    # Most recent first, so the page reads today-downwards.
    assert counts[0]["date"] > counts[-1]["date"]


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

def test_view_requires_staff(client, db):
    user = User.objects.create_user(username="ordinary_user_2", password="correctpassword")
    client.force_login(user)

    response = client.get(STATUS_URL)

    assert response.status_code == 302
    assert "/admin/login/" in response.url


def test_view_renders_the_snapshot_and_queue(staff_client, wired_redis, documents):
    publish_snapshot(np.array([1, 2, 3], dtype=HASH_DTYPE).tobytes(), wired_redis)
    enqueue_for_purge(documents, wired_redis)

    response = staff_client.get(STATUS_URL)
    content = response.content.decode()

    assert response.status_code == 200
    assert "https://badsite.test/a" in content
    assert "Purge queue" in content
    assert response.context["snapshot"]["blob_domains"] == 3
    assert response.context["queue"]["size"] == 2


def test_view_renders_when_redis_is_down(staff_client, monkeypatch):
    def explode():
        raise RedisConnectionError("connection refused")

    monkeypatch.setattr(admin_views.blacklist_snapshot, "get_redis", explode)

    response = staff_client.get(STATUS_URL)

    # A status page for Redis state is exactly what you load when Redis is down, so it
    # must report the failure rather than 500.
    assert response.status_code == 200
    assert "connection refused" in response.content.decode()
    assert "snapshot" not in response.context


def test_view_reports_the_background_tasks(staff_client, wired_redis):
    response = staff_client.get(STATUS_URL)

    task_names = [task["name"] for task in response.context["tasks"]]

    assert task_names == [admin_views.SNAPSHOT_TASK_NAME, admin_views.PURGE_TASK_NAME]
    # Nothing scheduled in the test DB, which is what a missing process_tasks worker looks
    # like - the page has to render that rather than assume the rows exist.
    assert all(task["task"] is None and task["last_completed"] is None
               for task in response.context["tasks"])


def test_admin_index_links_to_the_status_page(staff_client):
    response = staff_client.get("/admin/")

    assert STATUS_URL in response.content.decode()


# ---------------------------------------------------------------------------
# Curated domains
# ---------------------------------------------------------------------------

def test_curated_status_reports_nothing_still_blacklisted(wired_redis, monkeypatch):
    monkeypatch.setattr(admin_views, "get_curated_domains", lambda: {"pudding.cool"})
    publish_snapshot(np.array(hash_domains(["badsite.test"]), dtype=HASH_DTYPE).tobytes(), wired_redis)
    admin_views.get_snapshot_blacklist().load_now()

    status = admin_views._curated_status()

    assert status["count"] == 1
    assert status["still_blacklisted"] == []


def test_curated_status_flags_a_domain_the_snapshot_still_blocks(wired_redis, monkeypatch):
    """Either the rebuild has not run yet, or a local rule curation cannot override is
    catching it. Both are invisible without this."""
    monkeypatch.setattr(admin_views, "get_curated_domains", lambda: {"pudding.cool"})
    publish_snapshot(np.array(hash_domains(["pudding.cool"]), dtype=HASH_DTYPE).tobytes(), wired_redis)
    admin_views.get_snapshot_blacklist().load_now()

    status = admin_views._curated_status()

    assert status["still_blacklisted"] == ["pudding.cool"]
