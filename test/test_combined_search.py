"""Integration tests for the Combined Search endpoint (/api/v2/combined-search/).

The endpoint's own job is auth, quota, fetching Staan while the index is searched, and
handing both to one ranker. The ranking itself belongs to CombinedLTRRanker and is tested there, so these
tests stub the ranker and Staan: what is checked here
is that each source reaches the pool, that provenance survives as far as the wire, that a
provider being down costs recall rather than the request, and that the quota is enforced
atomically.
"""

import json
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from ninja_jwt.tokens import RefreshToken

import mwmbl.tinysearchengine.combined_search as combined_search
from mwmbl import pricing
from mwmbl.indexer import seed_crawl
from mwmbl.membership import MembershipTier
from mwmbl.models import ApiKey, Membership, UserBilling, generate_api_key
from mwmbl.quota import (
    _combined_search_api_monthly_key,
    _combined_search_monthly_key,
    _monthly_key,
    get_monthly_combined_search_api_count,
    get_monthly_count,
)
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, DocumentState, TinyIndex

User = get_user_model()

URL = "/api/v2/combined-search/"
USAGE_URL = "/api/v1/platform/combined-search/usage"

INDEX_RESULT = Document("Tokio internals", "https://blog.example.com/tokio", "A crawled page about tokio.")
STAAN_RESULT = Document(
    "Tokio", "https://tokio.rs/", "An asynchronous runtime for Rust.", 6.0, source=DocumentSource.STAAN
)
# There is no Wikipedia fetch, but the index holds Wikipedia pages of its own.
WIKI_INDEX_RESULT = Document(
    "Tokio (software)",
    "https://en.wikipedia.org/wiki/Tokio",
    "A Rust runtime.",
    state=DocumentState.FROM_WIKI,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Retrieval(list):
    """What the stub ranker retrieves: a list of index pages that a seed crawl can also read."""

    @property
    def pages(self):
        return list(self)


@pytest.fixture
def user(db):
    u = User.objects.create_user(username="csuser", email="cs@example.com", password="x")
    EmailAddress.objects.create(user=u, email="cs@example.com", verified=True, primary=True)
    return u


@pytest.fixture
def access_token(user):
    return str(RefreshToken.for_user(user).access_token)


@pytest.fixture
def api_key(user):
    raw, hashed = generate_api_key()
    key = ApiKey.objects.create(user=user, key=hashed, name="cs", scopes=[ApiKey.Scope.SEARCH])
    key.raw_key = raw
    return key


@pytest.fixture
def client(db):
    return Client()


@pytest.fixture
def fresh_quota(user):
    cache.delete(_combined_search_monthly_key(user.id))
    yield
    cache.delete(_combined_search_monthly_key(user.id))


@pytest.fixture
def stub_sources(monkeypatch):
    """Stub Staan and the ranker, recording what the ranker was given.

    The stub ranker returns the pool unchanged, so the response is the pool: that is what
    lets these tests assert on which sources reached it.
    """
    calls = {}

    def configure(staan=(STAAN_RESULT,), index=(INDEX_RESULT,), new_urls=(STAAN_RESULT.url,)):
        def fake_staan(query, *args, **kwargs):
            calls["staan_query"] = query
            if isinstance(staan, Exception):
                raise staan
            return list(staan)

        def fake_retrieve(query):
            calls["retrieve_query"] = query
            return _Retrieval(index)

        def fake_search_retrieved(retrieval, additional_results):
            calls["additional_results"] = additional_results
            return retrieval + list(additional_results)

        def fake_index(documents, query, path):
            calls["indexed"] = [document.url for document in documents]
            calls["indexed_query"] = query
            if isinstance(new_urls, Exception):
                raise new_urls
            return set(new_urls)

        monkeypatch.setattr(combined_search, "get_staan_results", fake_staan)
        monkeypatch.setattr(combined_search, "index_new_results_against_query", fake_index)
        monkeypatch.setattr(combined_search, "find_blacklisted_urls", lambda documents: set())
        # The router closed over the ranker at registration time, so the ranker instance
        # itself is what has to be patched, not the name in search_setup.
        from mwmbl.search_setup import combined_ranker

        monkeypatch.setattr(combined_ranker, "retrieve", fake_retrieve)
        monkeypatch.setattr(combined_ranker, "search_retrieved", fake_search_retrieved)
        return calls

    return configure


def _get(client, access_token, query="tokio"):
    return client.get(f"{URL}?q={query}", HTTP_AUTHORIZATION=f"Bearer {access_token}")


# ---------------------------------------------------------------------------
# Auth & quota
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_combined_search_requires_auth(client):
    assert client.get(f"{URL}?q=tokio").status_code == 401


@pytest.mark.django_db
def test_combined_search_with_jwt(client, access_token, fresh_quota, stub_sources):
    stub_sources()

    response = _get(client, access_token)

    assert response.status_code == 200
    assert response.json()["query"] == "tokio"


def _get_with_key(client, api_key):
    return client.get(f"{URL}?q=tokio", HTTP_X_API_KEY=api_key.raw_key)


@pytest.mark.django_db
def test_an_api_key_without_a_spend_limit_is_refused(client, api_key, fresh_quota, redis_cache, stub_sources):
    """There is no free allowance for keyed Combined Search, so it needs a spend limit."""
    stub_sources()

    response = _get_with_key(client, api_key)

    assert response.status_code == 402
    assert "spend limit" in response.json()["detail"]
    assert get_monthly_combined_search_api_count(api_key.user.id) == 0


@pytest.mark.django_db
def test_a_keyed_request_is_billed_on_its_own_counter(client, user, api_key, fresh_quota, redis_cache, stub_sources):
    stub_sources()
    UserBilling.objects.create(user=user, max_monthly_spend_cents=1_000)

    body = _get_with_key(client, api_key).json()

    assert body["monthly_usage"] == 1
    assert body["monthly_limit"] == pricing.combined_search_monthly_cap(1_000, 0)
    assert get_monthly_combined_search_api_count(user.id) == 1
    assert cache.get(_combined_search_monthly_key(user.id)) is None
    assert get_monthly_count(user.id) == 0


@pytest.mark.django_db
def test_a_member_using_a_key_is_billed_not_given_the_membership_quota(
    client, user, api_key, fresh_quota, redis_cache, stub_sources
):
    stub_sources()
    _join(user, MembershipTier.CANOPY)

    assert _get_with_key(client, api_key).status_code == 402


@pytest.mark.django_db
def test_a_keyed_request_over_the_spend_limit_is_refused_and_not_counted(
    client, user, api_key, fresh_quota, redis_cache, stub_sources
):
    stub_sources()
    UserBilling.objects.create(user=user, max_monthly_spend_cents=1_000)
    cap = pricing.combined_search_monthly_cap(1_000, 0)
    cache.set(_combined_search_api_monthly_key(user.id), cap, timeout=3600)

    response = _get_with_key(client, api_key)

    assert response.status_code == 429
    assert "spend limit" in response.json()["detail"]
    assert get_monthly_combined_search_api_count(user.id) == cap


@pytest.mark.django_db
def test_standard_search_overage_uses_up_the_shared_spend_limit(
    client, user, api_key, fresh_quota, redis_cache, stub_sources
):
    """$10 buys 2,000 overage requests of standard search; once they are used, nothing is
    left of the spend limit for Combined Search."""
    stub_sources()
    UserBilling.objects.create(user=user, max_monthly_spend_cents=1_000)
    cache.set(_monthly_key(user.id), pricing.FREE_KEYED_MONTHLY_LIMIT + 2_000, timeout=3600)

    assert _get_with_key(client, api_key).status_code == 429


@pytest.mark.django_db
@override_settings(COMBINED_SEARCH_MONTHLY_LIMITS={"free": 10})
def test_the_response_reports_the_quota(client, access_token, fresh_quota, stub_sources):
    stub_sources()

    body = _get(client, access_token).json()

    assert body["monthly_usage"] == 1
    assert body["monthly_limit"] == 10


@pytest.mark.django_db
@override_settings(COMBINED_SEARCH_MONTHLY_LIMITS={"free": 10})
def test_combined_search_quota_enforced(client, user, access_token, fresh_quota, stub_sources):
    stub_sources()
    cache.set(_combined_search_monthly_key(user.id), 10, timeout=3600)

    assert _get(client, access_token).status_code == 429


@pytest.mark.django_db
@override_settings(COMBINED_SEARCH_MONTHLY_LIMITS={"free": 10})
def test_a_rejected_request_refunds_its_increment(client, user, access_token, fresh_quota, stub_sources):
    """The counter is incremented before it is checked, so that concurrent requests cannot
    both pass. A rejected request must give that increment back, or being over the limit
    once would push the counter up forever."""
    stub_sources()
    key = _combined_search_monthly_key(user.id)
    cache.set(key, 10, timeout=3600)

    _get(client, access_token)

    assert cache.get(key) == 10


@pytest.mark.django_db
@override_settings(COMBINED_SEARCH_MONTHLY_LIMITS={"free": 10})
def test_the_quota_counter_is_its_own(client, user, access_token, fresh_quota, stub_sources):
    """Combined Search must not spend the standard-search or Super Search allowance."""
    from mwmbl.quota import get_monthly_super_search_count

    stub_sources()
    _get(client, access_token)

    assert get_monthly_count(user.id) == 0
    assert get_monthly_super_search_count(user.id) == 0


def _join(user, tier):
    Membership.objects.create(user=user, tier=tier, polar_subscription_id=f"sub_{tier}")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tier, limit",
    [(None, 30), (MembershipTier.SPROUT, 300), (MembershipTier.SAPLING, 1_500), (MembershipTier.CANOPY, 1_500)],
)
def test_the_limit_is_set_by_the_membership_tier(client, user, access_token, fresh_quota, stub_sources, tier, limit):
    stub_sources()
    if tier is not None:
        _join(user, tier)
    key = _combined_search_monthly_key(user.id)

    cache.set(key, limit - 1, timeout=3600)
    last_allowed = _get(client, access_token)
    over_the_limit = _get(client, access_token)

    assert last_allowed.status_code == 200
    assert last_allowed.json()["monthly_limit"] == limit
    assert over_the_limit.status_code == 429


@pytest.mark.django_db
def test_the_usage_endpoint_reports_the_members_quota(client, user, access_token, fresh_quota):
    _join(user, MembershipTier.SAPLING)
    cache.set(_combined_search_monthly_key(user.id), 7, timeout=3600)

    response = client.get(USAGE_URL, HTTP_AUTHORIZATION=f"Bearer {access_token}")

    assert response.status_code == 200
    assert response.json() == {"monthly_usage": 7, "monthly_limit": 1_500}


@pytest.mark.django_db
def test_the_usage_endpoint_reports_the_default_quota_for_a_non_member(client, access_token, fresh_quota):
    response = client.get(USAGE_URL, HTTP_AUTHORIZATION=f"Bearer {access_token}")

    assert response.json() == {"monthly_usage": 0, "monthly_limit": 30}


@pytest.mark.django_db
def test_the_usage_endpoint_requires_a_jwt(client):
    assert client.get(USAGE_URL).status_code == 401


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_both_sources_reach_the_response(client, access_token, fresh_quota, stub_sources):
    stub_sources()

    results = _get(client, access_token).json()["results"]

    assert [result["url"] for result in results] == [INDEX_RESULT.url, STAAN_RESULT.url]


@pytest.mark.django_db
def test_each_result_names_the_provider_it_came_from(client, access_token, fresh_quota, stub_sources):
    stub_sources(index=(INDEX_RESULT, WIKI_INDEX_RESULT))

    results = _get(client, access_token).json()["results"]

    assert [result["engine"] for result in results] == ["mwmbl", "wikipedia", "eusp"]


@pytest.mark.django_db
def test_the_external_results_are_passed_in_as_additional_results(client, access_token, fresh_quota, stub_sources):
    """Staan goes in through Ranker.get_results' additional_results hook, which is what gets
    it blacklist-filtered and ranked alongside the index candidates."""
    calls = stub_sources()

    _get(client, access_token)

    assert [document.url for document in calls["additional_results"]] == [STAAN_RESULT.url]


@pytest.mark.django_db
def test_the_index_is_searched_while_staan_is_in_flight(client, access_token, fresh_quota, stub_sources, monkeypatch):
    """Staan is the slow call, so the index lookup must overlap it, not wait for it. Each
    stub waits for the other to start: run one after the other, the first would time out."""
    stub_sources()
    staan_started = threading.Event()
    retrieve_started = threading.Event()
    from mwmbl.search_setup import combined_ranker

    fake_retrieve = combined_ranker.retrieve
    fake_staan = combined_search.get_staan_results

    def overlapping_retrieve(query):
        retrieve_started.set()
        assert staan_started.wait(timeout=5)
        return fake_retrieve(query)

    def overlapping_staan(query, *args, **kwargs):
        staan_started.set()
        assert retrieve_started.wait(timeout=5)
        return fake_staan(query, *args, **kwargs)

    monkeypatch.setattr(combined_ranker, "retrieve", overlapping_retrieve)
    monkeypatch.setattr(combined_search, "get_staan_results", overlapping_staan)

    results = _get(client, access_token).json()["results"]

    assert [result["url"] for result in results] == [INDEX_RESULT.url, STAAN_RESULT.url]


@pytest.mark.django_db
def test_the_query_reaches_staan(client, access_token, fresh_quota, stub_sources):
    calls = stub_sources()

    _get(client, access_token, query="rust")

    assert calls["staan_query"] == "rust"
    assert calls["retrieve_query"] == "rust"


@pytest.mark.django_db
def test_the_result_count_matches_the_results(client, access_token, fresh_quota, stub_sources):
    stub_sources()

    body = _get(client, access_token).json()

    assert body["number_of_results"] == len(body["results"])


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staan_returning_nothing_still_serves_the_index(client, access_token, fresh_quota, stub_sources):
    stub_sources(staan=())

    results = _get(client, access_token).json()["results"]

    assert [result["url"] for result in results] == [INDEX_RESULT.url]


@pytest.mark.django_db
def test_an_empty_pool_is_an_empty_response_not_an_error(client, access_token, fresh_quota, stub_sources):
    stub_sources(staan=(), index=())

    body = _get(client, access_token).json()

    assert body["results"] == []
    assert body["number_of_results"] == 0


# ---------------------------------------------------------------------------
# Indexing Staan's results
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staan_results_are_indexed_against_the_query(client, access_token, fresh_quota, stub_sources):
    calls = stub_sources(new_urls=("https://a.example/", "https://b.example/", "https://c.example/"))

    body = _get(client, access_token, query="rust").json()

    assert calls["indexed"] == [STAAN_RESULT.url]
    assert calls["indexed_query"] == "rust"
    assert body["pages_indexed"] == 3


@pytest.mark.django_db
def test_blacklisted_staan_results_are_not_indexed(client, access_token, fresh_quota, stub_sources, monkeypatch):
    """index_new_results_against_query bypasses index_documents' blacklist check."""
    bad = Document("Bad", "https://badsite.test/x", "bad", 5.0, source=DocumentSource.STAAN)
    calls = stub_sources(staan=(bad, STAAN_RESULT))
    monkeypatch.setattr(
        combined_search,
        "find_blacklisted_urls",
        lambda documents: {d.url for d in documents if urlparse(d.url).netloc == "badsite.test"},
    )

    _get(client, access_token)

    assert calls["indexed"] == [STAAN_RESULT.url]


@pytest.mark.django_db
def test_nothing_from_staan_indexes_nothing(client, access_token, fresh_quota, stub_sources):
    calls = stub_sources(staan=())

    body = _get(client, access_token).json()

    assert "indexed" not in calls
    assert body["pages_indexed"] == 0


@pytest.mark.django_db
def test_a_failed_index_write_still_serves_the_results(client, access_token, fresh_quota, stub_sources):
    stub_sources(new_urls=OSError("disk full"))

    body = _get(client, access_token).json()

    assert body["pages_indexed"] == 0
    assert [result["url"] for result in body["results"]] == [INDEX_RESULT.url, STAAN_RESULT.url]


def test_index_staan_results_writes_new_pages_once(tmp_path, monkeypatch):
    index_path = Path(tmp_path) / "index.tinysearch"
    with TinyIndex.create(Document, str(index_path), num_pages=64, page_size=4096):
        pass
    monkeypatch.setattr(combined_search, "index_path", index_path)
    monkeypatch.setattr(combined_search, "find_blacklisted_urls", lambda documents: set())

    assert combined_search.index_staan_results("tokio", [STAAN_RESULT]) == {STAAN_RESULT.url}
    assert combined_search.index_staan_results("tokio", [STAAN_RESULT]) == set()

    with TinyIndex(Document, str(index_path), "r") as index:
        assert [document.url for document in index.retrieve("tokio")] == [STAAN_RESULT.url]


# ---------------------------------------------------------------------------
# Seed crawls (the crawl itself is tested in test_seed_crawl.py)
# ---------------------------------------------------------------------------

NEW_PAGES_URL = f"{URL}new-pages"


@pytest.fixture
def crawl_sources(stub_sources, monkeypatch):
    monkeypatch.setattr(seed_crawl, "find_blacklisted_urls", lambda documents: set())
    return stub_sources


def _crawl(client, access_token, query="tokio"):
    return client.get(f"{URL}?q={query}&crawl=true", HTTP_AUTHORIZATION=f"Bearer {access_token}")


@pytest.mark.django_db
def test_crawl_schedules_a_seed_crawl_of_what_the_index_lacked(
    client, user, access_token, fresh_quota, redis_cache, crawl_sources
):
    crawl_sources()

    body = _crawl(client, access_token).json()

    assert body["crawl_scheduled"] is True
    assert body["crawl_outcome"] == "scheduled"
    job = json.loads(redis_cache.rpop(seed_crawl.QUEUE_KEY))
    assert job == {
        "user_id": user.id,
        "query": "tokio",
        "seed_urls": [STAAN_RESULT.url],
        "new_seed_urls": [STAAN_RESULT.url],
        "domains": {"tokio.rs": 1},
    }
    assert seed_crawl.get_seed_crawl(user.id, "tokio")["status"] == "queued"


@pytest.mark.django_db
def test_no_crawl_without_the_flag(client, access_token, fresh_quota, redis_cache, crawl_sources):
    crawl_sources()

    body = _get(client, access_token).json()

    assert body["crawl_scheduled"] is False
    assert body["crawl_outcome"] is None
    assert redis_cache.llen(seed_crawl.QUEUE_KEY) == 0


@pytest.mark.django_db
def test_no_crawl_when_the_index_already_has_every_staan_result(
    client, access_token, fresh_quota, redis_cache, crawl_sources
):
    crawl_sources(index=(INDEX_RESULT, STAAN_RESULT))

    body = _crawl(client, access_token).json()

    assert body["crawl_scheduled"] is False
    assert body["crawl_outcome"] == "already_indexed"
    assert redis_cache.llen(seed_crawl.QUEUE_KEY) == 0


@pytest.mark.django_db
def test_a_crawl_already_running_is_named(client, access_token, fresh_quota, redis_cache, crawl_sources):
    crawl_sources()
    _crawl(client, access_token, query="tokio")

    body = _crawl(client, access_token, query="rust").json()

    assert body["crawl_scheduled"] is False
    assert body["crawl_outcome"] == "already_running"
    assert body["active_crawl_query"] == "tokio"


@pytest.mark.django_db
def test_crawl_is_refused_with_an_api_key_and_not_counted(
    client, user, api_key, fresh_quota, redis_cache, crawl_sources
):
    crawl_sources()
    UserBilling.objects.create(user=user, max_monthly_spend_cents=1_000)

    response = client.get(f"{URL}?q=tokio&crawl=true", HTTP_X_API_KEY=api_key.raw_key)

    assert response.status_code == 403
    assert get_monthly_combined_search_api_count(user.id) == 0


@pytest.mark.django_db
def test_new_pages_reports_the_crawl_for_the_query(client, access_token, fresh_quota, redis_cache, crawl_sources):
    crawl_sources()
    _crawl(client, access_token)

    response = client.get(f"{NEW_PAGES_URL}?q=tokio", HTTP_AUTHORIZATION=f"Bearer {access_token}")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["pages"] == []


@pytest.mark.django_db
def test_new_pages_is_404_without_a_crawl(client, access_token, redis_cache):
    response = client.get(f"{NEW_PAGES_URL}?q=tokio", HTTP_AUTHORIZATION=f"Bearer {access_token}")

    assert response.status_code == 404


@pytest.mark.django_db
def test_new_pages_only_shows_the_users_own_crawls(client, access_token, fresh_quota, redis_cache, crawl_sources):
    crawl_sources()
    _crawl(client, access_token)
    other = User.objects.create_user(username="other", email="other@example.com", password="x")
    other_token = str(RefreshToken.for_user(other).access_token)

    response = client.get(f"{NEW_PAGES_URL}?q=tokio", HTTP_AUTHORIZATION=f"Bearer {other_token}")

    assert response.status_code == 404


@pytest.mark.django_db
def test_new_pages_requires_a_jwt(client, api_key):
    assert client.get(f"{NEW_PAGES_URL}?q=tokio", HTTP_X_API_KEY=api_key.raw_key).status_code == 401


@pytest.mark.django_db
def test_new_pages_count_reports_how_many_pages_the_crawl_added(
    client, user, access_token, fresh_quota, redis_cache, crawl_sources
):
    crawl_sources()
    _crawl(client, access_token)
    redis_cache.rpush(f"{seed_crawl._record_key(user.id, 'tokio')}:pages", '{"url": "u", "title": "t", "extract": ""}')

    response = client.get(f"{NEW_PAGES_URL}/count?q=tokio", HTTP_AUTHORIZATION=f"Bearer {access_token}")

    assert response.status_code == 200
    assert response.json()["pages_indexed"] == 1
    assert response.json()["status"] == "queued"
    assert "pages" not in response.json()


@pytest.mark.django_db
def test_new_pages_count_is_404_without_a_crawl(client, access_token, redis_cache):
    response = client.get(f"{NEW_PAGES_URL}/count?q=tokio", HTTP_AUTHORIZATION=f"Bearer {access_token}")

    assert response.status_code == 404


@pytest.mark.django_db
def test_new_pages_count_requires_a_jwt(client, api_key):
    assert client.get(f"{NEW_PAGES_URL}/count?q=tokio", HTTP_X_API_KEY=api_key.raw_key).status_code == 401
