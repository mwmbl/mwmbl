"""Integration tests for the Combined Search endpoint (/api/v2/combined-search/).

The endpoint's own job is auth, quota, fetching Staan and handing it to one ranker alongside
the index. The ranking itself belongs to CombinedLTRRanker and is tested there, so these
tests stub the ranker and Staan: what is checked here
is that each source reaches the pool, that provenance survives as far as the wire, that a
provider being down costs recall rather than the request, and that the quota is enforced
atomically.
"""

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from ninja_jwt.tokens import RefreshToken

import mwmbl.tinysearchengine.combined_search as combined_search
from mwmbl.models import ApiKey, generate_api_key
from mwmbl.quota import _combined_search_monthly_key
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, DocumentState

User = get_user_model()

URL = "/api/v2/combined-search/"

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
def fresh_quota(api_key):
    cache.delete(_combined_search_monthly_key(api_key.user.id))
    yield
    cache.delete(_combined_search_monthly_key(api_key.user.id))


@pytest.fixture
def stub_sources(monkeypatch):
    """Stub Staan and the ranker, recording what the ranker was given.

    The stub ranker returns the pool unchanged, so the response is the pool: that is what
    lets these tests assert on which sources reached it.
    """
    calls = {}

    def configure(staan=(STAAN_RESULT,), index=(INDEX_RESULT,)):
        def fake_staan(query, *args, **kwargs):
            calls["staan_query"] = query
            if isinstance(staan, Exception):
                raise staan
            return list(staan)

        def fake_search(query, additional_results, use_external_search=True):
            calls["additional_results"] = additional_results
            calls["use_external_search"] = use_external_search
            return list(index) + list(additional_results)

        monkeypatch.setattr(combined_search, "get_staan_results", fake_staan)
        # The router closed over the ranker at registration time, so the ranker instance
        # itself is what has to be patched, not the name in search_setup.
        from mwmbl.search_setup import combined_ranker

        monkeypatch.setattr(combined_ranker, "search", fake_search)
        return calls

    return configure


def _get(client, api_key, query="tokio"):
    return client.get(f"{URL}?q={query}", HTTP_X_API_KEY=api_key.raw_key)


# ---------------------------------------------------------------------------
# Auth & quota
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_combined_search_requires_auth(client):
    assert client.get(f"{URL}?q=tokio").status_code == 401


@pytest.mark.django_db
def test_combined_search_with_api_key(client, api_key, fresh_quota, stub_sources):
    stub_sources()

    response = _get(client, api_key)

    assert response.status_code == 200
    assert response.json()["query"] == "tokio"


@pytest.mark.django_db
def test_combined_search_with_jwt(client, user, access_token, fresh_quota, stub_sources):
    stub_sources()

    response = client.get(f"{URL}?q=tokio", HTTP_AUTHORIZATION=f"Bearer {access_token}")

    assert response.status_code == 200


@pytest.mark.django_db
@override_settings(COMBINED_SEARCH_MONTHLY_LIMIT=10)
def test_the_response_reports_the_quota(client, api_key, fresh_quota, stub_sources):
    stub_sources()

    body = _get(client, api_key).json()

    assert body["monthly_usage"] == 1
    assert body["monthly_limit"] == 10


@pytest.mark.django_db
@override_settings(COMBINED_SEARCH_MONTHLY_LIMIT=10)
def test_combined_search_quota_enforced(client, api_key, fresh_quota, stub_sources):
    stub_sources()
    cache.set(_combined_search_monthly_key(api_key.user.id), 10, timeout=3600)

    assert _get(client, api_key).status_code == 429


@pytest.mark.django_db
@override_settings(COMBINED_SEARCH_MONTHLY_LIMIT=10)
def test_a_rejected_request_refunds_its_increment(client, api_key, fresh_quota, stub_sources):
    """The counter is incremented before it is checked, so that concurrent requests cannot
    both pass. A rejected request must give that increment back, or being over the limit
    once would push the counter up forever."""
    stub_sources()
    key = _combined_search_monthly_key(api_key.user.id)
    cache.set(key, 10, timeout=3600)

    _get(client, api_key)

    assert cache.get(key) == 10


@pytest.mark.django_db
@override_settings(COMBINED_SEARCH_MONTHLY_LIMIT=10)
def test_the_quota_counter_is_its_own(client, api_key, fresh_quota, stub_sources):
    """Combined Search must not spend the standard-search or Super Search allowance."""
    from mwmbl.quota import get_monthly_count, get_monthly_super_search_count

    stub_sources()
    _get(client, api_key)

    assert get_monthly_count(api_key.user.id) == 0
    assert get_monthly_super_search_count(api_key.user.id) == 0


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_both_sources_reach_the_response(client, api_key, fresh_quota, stub_sources):
    stub_sources()

    results = _get(client, api_key).json()["results"]

    assert [result["url"] for result in results] == [INDEX_RESULT.url, STAAN_RESULT.url]


@pytest.mark.django_db
def test_each_result_names_the_provider_it_came_from(client, api_key, fresh_quota, stub_sources):
    stub_sources(index=(INDEX_RESULT, WIKI_INDEX_RESULT))

    results = _get(client, api_key).json()["results"]

    assert [result["engine"] for result in results] == ["mwmbl", "wikipedia", "staan"]


@pytest.mark.django_db
def test_the_external_results_are_passed_in_as_additional_results(client, api_key, fresh_quota, stub_sources):
    """Staan goes in through Ranker.get_results' additional_results hook, which is what gets
    it blacklist-filtered and ranked alongside the index candidates."""
    calls = stub_sources()

    _get(client, api_key)

    assert [document.url for document in calls["additional_results"]] == [STAAN_RESULT.url]


@pytest.mark.django_db
def test_the_ranker_is_told_not_to_fetch_wikipedia(client, api_key, fresh_quota, stub_sources):
    """External search is standard search's Wikipedia fetch, which Combined Search drops:
    Staan already returns Wikipedia pages when they are relevant."""
    calls = stub_sources()

    _get(client, api_key)

    assert calls["use_external_search"] is False


@pytest.mark.django_db
def test_the_query_reaches_staan(client, api_key, fresh_quota, stub_sources):
    calls = stub_sources()

    _get(client, api_key, query="rust")

    assert calls["staan_query"] == "rust"


@pytest.mark.django_db
def test_the_result_count_matches_the_results(client, api_key, fresh_quota, stub_sources):
    stub_sources()

    body = _get(client, api_key).json()

    assert body["number_of_results"] == len(body["results"])


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staan_returning_nothing_still_serves_the_index(client, api_key, fresh_quota, stub_sources):
    stub_sources(staan=())

    results = _get(client, api_key).json()["results"]

    assert [result["url"] for result in results] == [INDEX_RESULT.url]


@pytest.mark.django_db
def test_an_empty_pool_is_an_empty_response_not_an_error(client, api_key, fresh_quota, stub_sources):
    stub_sources(staan=(), index=())

    body = _get(client, api_key).json()

    assert body["results"] == []
    assert body["number_of_results"] == 0
