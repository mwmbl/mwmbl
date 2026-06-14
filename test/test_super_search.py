"""Integration tests for the Super Search streaming endpoint.

These tests:
- Mock external sources and `crawl_url` so no network is touched.
- Stub the LTR scoring helper so promotion is deterministic.
- Verify quota, auth, event ordering, and forbidden-symbol presence in
  the orchestrator (architecture guard).
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import ApiKey, generate_api_key
from mwmbl.quota import _super_search_monthly_key
from mwmbl.tinysearchengine.indexer import Document

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def user(db):
    u = User.objects.create_user(username="ssuser", email="ss@example.com", password="x")
    EmailAddress.objects.create(user=u, email="ss@example.com", verified=True, primary=True)
    return u


@pytest.fixture
def access_token(user):
    return str(RefreshToken.for_user(user).access_token)


@pytest.fixture
def api_key(user):
    raw, h = generate_api_key()
    obj = ApiKey.objects.create(user=user, key=h, name="ss", scopes=[ApiKey.Scope.SEARCH])
    obj.raw_key = raw
    return obj


@pytest.fixture
def client(db):
    return Client()


def _parse_sse(body: bytes) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for chunk in body.split(b"\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk.startswith(b":"):
            continue
        event_type = None
        data = None
        for line in chunk.split(b"\n"):
            if line.startswith(b"event: "):
                event_type = line[len(b"event: "):].decode()
            elif line.startswith(b"data: "):
                data = json.loads(line[len(b"data: "):].decode())
        if event_type is not None:
            events.append((event_type, data))
    return events


def _read_stream(response) -> bytes:
    content = response.streaming_content
    if hasattr(content, "__aiter__"):
        async def _collect():
            return b"".join([chunk async for chunk in content])
        return async_to_sync(_collect)()
    return b"".join(content)


def _stub_sources(monkeypatch, by_source: dict[str, list[Document]]):
    """Replace the live source adapters with deterministic stubs."""
    import mwmbl.tinysearchengine.super_search as ss

    new_sources = {}
    for name, docs in by_source.items():
        async def fake_search(client, query, limit, _docs=docs):
            return _docs
        new_sources[name] = fake_search
    monkeypatch.setattr(ss, "SOURCES", new_sources)


def _stub_scoring(monkeypatch, scores: list[float]):
    """Stub both the promotion scorer (_heuristic_score_docs) and the LTR final
    ranker (score_documents) to consume from a single shared score iterator.
    Promotion scores are consumed first; remaining scores go to final ranking."""
    import mwmbl.tinysearchengine.super_search as ss

    iterator = iter(scores)

    def fake_promote(query, docs):
        return [next(iterator, 0.0) for _ in docs]

    def fake_ltr(model, query, docs):
        return [next(iterator, 0.0) for _ in docs]

    monkeypatch.setattr(ss, "_heuristic_score_docs", fake_promote)
    monkeypatch.setattr(ss, "score_documents", fake_ltr)


# ---------------------------------------------------------------------------
# Auth & quota
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_super_search_requires_auth(client):
    response = client.get("/api/v2/super-search/?q=python")
    assert response.status_code == 401


@pytest.mark.django_db
def test_super_search_with_api_key(client, api_key, monkeypatch):
    cache.delete(_super_search_monthly_key(api_key.user.id))
    _stub_sources(monkeypatch, {"hn": []})
    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert response.status_code == 200
    events = _parse_sse(_read_stream(response))
    assert any(t == "done" for t, _ in events)


@pytest.mark.django_db
def test_super_search_with_jwt(client, access_token, user, monkeypatch):
    cache.delete(_super_search_monthly_key(user.id))
    _stub_sources(monkeypatch, {"hn": []})
    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )
    assert response.status_code == 200
    _read_stream(response)


@pytest.mark.django_db
@override_settings(SUPER_SEARCH_MONTHLY_LIMIT=10)
def test_super_search_quota_enforced(client, api_key, monkeypatch):
    cache.set(_super_search_monthly_key(api_key.user.id), 10, timeout=3600)
    _stub_sources(monkeypatch, {"hn": []})
    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert response.status_code == 429
    cache.delete(_super_search_monthly_key(api_key.user.id))


# ---------------------------------------------------------------------------
# Event-stream behaviour
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@override_settings(SUPER_SEARCH_TOP_K=2)
def test_promoted_results_use_top_k(client, api_key, monkeypatch):
    """Exactly top-K docs are promoted for crawling; surplus docs are excluded."""
    cache.delete(_super_search_monthly_key(api_key.user.id))
    _stub_sources(monkeypatch, {
        "hn": [
            Document(title="Best",   url="https://best.example/",   extract="x"),
            Document(title="Second", url="https://second.example/", extract="x"),
            Document(title="Third",  url="https://third.example/",  extract="x"),
        ],
    })
    # Scores in descending order — only the top-2 should be promoted.
    _stub_scoring(monkeypatch, [0.9, 0.5, 0.1] + [0.0] * 20)

    def fake_crawl(url, redis=None):
        return {"url": url, "status": 200, "timestamp": 0, "content": None, "error": None}

    monkeypatch.setattr("mwmbl.tinysearchengine.super_search.crawl_url", fake_crawl)

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert response.status_code == 200
    events = _parse_sse(_read_stream(response))
    event_types = [t for t, _ in events]

    assert "source_started" in event_types
    assert "source_returned" in event_types
    promoted = [d for t, d in events if t == "result_promoted"]
    promoted_urls = {p["url"] for p in promoted}
    assert len(promoted) == 2
    assert "https://best.example/" in promoted_urls
    assert "https://second.example/" in promoted_urls
    assert "https://third.example/" not in promoted_urls
    assert event_types[-1] == "done"


@pytest.mark.django_db
@override_settings(SUPER_SEARCH_TOP_K=2)
def test_heap_replacement_promotes_better_late_doc(client, api_key, monkeypatch):
    """When the heap is full, a later doc with a higher score replaces the minimum and
    is still promoted — the heap min-replacement path must work."""
    cache.delete(_super_search_monthly_key(api_key.user.id))
    _stub_sources(monkeypatch, {
        "hn": [
            Document(title="Low A",  url="https://low-a.example/",  extract="x"),
            Document(title="Low B",  url="https://low-b.example/",  extract="x"),
            Document(title="Low C",  url="https://low-c.example/",  extract="x"),
            Document(title="Best",   url="https://best.example/",   extract="x"),
        ],
    })
    # First two fill the heap at 0.1; Low C can't enter (0.1 > 0.1 is False);
    # Best (0.9) beats the minimum and must be promoted.
    _stub_scoring(monkeypatch, [0.1, 0.1, 0.1, 0.9] + [0.0] * 20)

    def fake_crawl(url, redis=None):
        return {"url": url, "status": 200, "timestamp": 0, "content": None, "error": None}

    monkeypatch.setattr("mwmbl.tinysearchengine.super_search.crawl_url", fake_crawl)

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert response.status_code == 200
    events = _parse_sse(_read_stream(response))
    promoted_urls = {d["url"] for t, d in events if t == "result_promoted"}

    assert "https://best.example/" in promoted_urls, "High-score late doc must be promoted"
    assert "https://low-c.example/" not in promoted_urls, "Equal-score doc must not displace heap entries"


@pytest.mark.django_db
@override_settings(SUPER_SEARCH_TOP_K=2)
def test_heap_equal_score_does_not_enter_full_heap(client, api_key, monkeypatch):
    """A doc whose score equals the heap minimum must NOT be promoted — the check is
    strictly greater-than, so ties don't displace existing entries."""
    cache.delete(_super_search_monthly_key(api_key.user.id))
    _stub_sources(monkeypatch, {
        "hn": [
            Document(title="First",  url="https://first.example/",  extract="x"),
            Document(title="Second", url="https://second.example/", extract="x"),
            Document(title="Third",  url="https://third.example/",  extract="x"),
        ],
    })
    _stub_scoring(monkeypatch, [0.5, 0.5, 0.5] + [0.0] * 20)

    def fake_crawl(url, redis=None):
        return {"url": url, "status": 200, "timestamp": 0, "content": None, "error": None}

    monkeypatch.setattr("mwmbl.tinysearchengine.super_search.crawl_url", fake_crawl)

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    events = _parse_sse(_read_stream(response))
    promoted = [d for t, d in events if t == "result_promoted"]
    promoted_urls = {p["url"] for p in promoted}

    assert len(promoted) == 2
    assert "https://third.example/" not in promoted_urls


@pytest.mark.django_db
def test_final_results_event_emitted(client, api_key, monkeypatch):
    """A 'results' event with the full ranked list is emitted before 'done'."""
    cache.delete(_super_search_monthly_key(api_key.user.id))
    _stub_sources(monkeypatch, {
        "hn": [Document(title="Python intro", url="https://py.example/", extract="A guide")],
    })
    _stub_scoring(monkeypatch, [0.5] + [0.0] * 20)

    def fake_crawl(url, redis=None):
        return {"url": url, "status": 200, "timestamp": 0, "content": None, "error": None}

    monkeypatch.setattr("mwmbl.tinysearchengine.super_search.crawl_url", fake_crawl)

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert response.status_code == 200
    events = _parse_sse(_read_stream(response))
    event_types = [t for t, _ in events]

    assert "results" in event_types
    results_events = [d for t, d in events if t == "results"]
    assert len(results_events) >= 1
    # The last results event is the authoritative final ranking.
    last_results = results_events[-1]["results"]
    assert len(last_results) >= 1
    assert last_results[0]["url"] == "https://py.example/"
    # At least one 'results' must appear before 'done'
    assert event_types.index("results") < event_types.index("done")


@pytest.mark.django_db
def test_done_reports_pages_indexed(client, api_key, monkeypatch):
    """Results are indexed at the end and the count is reported in 'done'."""
    import mwmbl.tinysearchengine.super_search as ss

    cache.delete(_super_search_monthly_key(api_key.user.id))
    _stub_sources(monkeypatch, {
        "hn": [Document(title="Python intro", url="https://py.example/", extract="A guide")],
    })
    _stub_scoring(monkeypatch, [0.5] + [0.0] * 20)
    monkeypatch.setattr(
        "mwmbl.tinysearchengine.super_search.crawl_url",
        lambda url: {"url": url, "status": 200, "timestamp": 0, "content": None, "error": None},
    )

    captured = {}

    def fake_index(documents, query, index_path):
        captured["query"] = query
        captured["urls"] = {d.url for d in documents}
        return 4

    monkeypatch.setattr(ss, "index_results_against_query", fake_index)

    response = client.get("/api/v2/super-search/?q=python", HTTP_X_API_KEY=api_key.raw_key)
    assert response.status_code == 200
    events = _parse_sse(_read_stream(response))

    done = next(d for t, d in events if t == "done")
    assert done["reason"] == "complete"
    assert done["pages_indexed"] == 4
    # The collected results are what gets indexed, against the original query.
    assert captured["query"] == "python"
    assert "https://py.example/" in captured["urls"]


@pytest.mark.django_db
def test_source_failure_emits_source_failed(client, api_key, monkeypatch):
    cache.delete(_super_search_monthly_key(api_key.user.id))

    import mwmbl.tinysearchengine.super_search as ss

    async def bad_source(client, query, limit):
        raise RuntimeError("boom")

    monkeypatch.setattr(ss, "SOURCES", {"hn": bad_source})

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    events = _parse_sse(_read_stream(response))
    types = [t for t, _ in events]
    assert "source_failed" in types
    assert types[-1] == "done"


@pytest.mark.django_db
@override_settings(SUPER_SEARCH_MONTHLY_LIMIT=2)
def test_quota_increment_refunded_when_over_limit(client, api_key, monkeypatch):
    """An over-limit request is rejected and the increment is refunded, so the
    stored counter is not left permanently inflated (increment-first + refund)."""
    key = _super_search_monthly_key(api_key.user.id)
    cache.set(key, 2, timeout=3600)  # already at the limit
    _stub_sources(monkeypatch, {"hn": []})

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert response.status_code == 429
    assert cache.get(key) == 2, "the rejected request must not leave the counter incremented"
    cache.delete(key)


@pytest.mark.django_db
@override_settings(SUPER_SEARCH_DEADLINE_SECONDS=0.05, SUPER_SEARCH_PER_SOURCE_TIMEOUT=2.0)
def test_pipeline_deadline_times_out(client, api_key, monkeypatch):
    """When the pipeline overruns the deadline, the stream still terminates with
    a `done` event whose reason is `timed_out`."""
    cache.delete(_super_search_monthly_key(api_key.user.id))

    import mwmbl.tinysearchengine.super_search as ss

    async def slow_source(client, query, limit):
        await asyncio.sleep(0.5)
        return []

    monkeypatch.setattr(ss, "SOURCES", {"hn": slow_source})

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    events = _parse_sse(_read_stream(response))
    assert events[-1][0] == "done"
    assert events[-1][1]["reason"] == "timed_out"


@pytest.mark.django_db
@override_settings(SUPER_SEARCH_TOP_K=2)
def test_link_following_adds_followed_docs(client, api_key, monkeypatch):
    """A promoted page is crawled, its outbound links followed, and the followed
    docs appear in the final ranking — exercises page_fetched / link_followed."""
    cache.delete(_super_search_monthly_key(api_key.user.id))
    _stub_sources(monkeypatch, {
        "hn": [Document(title="Python parent", url="https://parent.example/",
                        extract="python parent text")],
    })
    _stub_scoring(monkeypatch, [0.9] + [0.0] * 20)

    def fake_crawl(url, redis=None):
        if url == "https://parent.example/":
            return {"url": url, "status": 200, "content": {
                "title": "Python parent", "extract": "python parent text",
                "links": ["https://child.example/python-guide"], "extra_links": [],
            }}
        return {"url": url, "status": 200, "content": {
            "title": "Python child", "extract": "about python", "links": [], "extra_links": [],
        }}

    monkeypatch.setattr("mwmbl.tinysearchengine.super_search.crawl_url", fake_crawl)

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert response.status_code == 200
    events = _parse_sse(_read_stream(response))
    types = [t for t, _ in events]

    assert "page_fetched" in types
    followed = [d for t, d in events if t == "link_followed"]
    assert any(d["url"] == "https://child.example/python-guide" and d["from"] == "https://parent.example/"
               for d in followed)

    final = [d for t, d in events if t == "results"][-1]["results"]
    final_urls = {r["url"] for r in final}
    assert "https://child.example/python-guide" in final_urls


@pytest.mark.django_db
@override_settings(SUPER_SEARCH_TOP_K=3)
def test_no_duplicate_consecutive_results_frames(client, api_key, monkeypatch):
    """The dedup guard must never emit two consecutive identical `results` frames."""
    cache.delete(_super_search_monthly_key(api_key.user.id))
    _stub_sources(monkeypatch, {
        "hn": [
            Document(title="Python one", url="https://one.example/", extract="python one"),
            Document(title="Python two", url="https://two.example/", extract="python two"),
        ],
    })
    _stub_scoring(monkeypatch, [0.9, 0.8] + [0.0] * 40)

    def fake_crawl(url, redis=None):
        return {"url": url, "status": 200, "content": None}

    monkeypatch.setattr("mwmbl.tinysearchengine.super_search.crawl_url", fake_crawl)

    response = client.get(
        "/api/v2/super-search/?q=python",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    events = _parse_sse(_read_stream(response))
    results_keys = [tuple(r["url"] for r in d["results"]) for t, d in events if t == "results"]
    for prev, nxt in zip(results_keys, results_keys[1:]):
        assert prev != nxt, "consecutive identical results frames must be deduplicated"


# ---------------------------------------------------------------------------
# Architecture guard: no forbidden sync symbols in the orchestrator
# ---------------------------------------------------------------------------

def test_super_search_module_has_no_blocking_imports():
    """A failsafe against accidentally importing the sync `requests` lib
    or using `time.sleep` at module scope in the async orchestrator.
    """
    path = Path(__file__).resolve().parent.parent / "mwmbl" / "tinysearchengine" / "super_search.py"
    source = path.read_text()
    assert "\nimport requests" not in source and "\nfrom requests " not in source
    assert "time.sleep" not in source
