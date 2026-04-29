"""
Tests for search API key management and quota enforcement.

Covers:
- POST/GET/DELETE /api/v1/platform/api-keys/
- GET /api/v2/search/ with valid/invalid/wrong-scope keys
- Monthly quota and per-second rate-limit enforcement
- POST /api/v1/crawler/results with header vs body key and scope checks
- flush_search_counts background task
"""

from datetime import datetime
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from mwmbl.background import sync_search_counts
from mwmbl.models import ApiKey, MwmblUser, UsageBucket, generate_api_key
from mwmbl.quota import RATE_LIMIT, _monthly_key, check_rate_limit, get_monthly_count, increment_monthly

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def verified_user(db):
    user = User.objects.create_user(
        username="searchuser",
        email="search@example.com",
        password="testpass123",
    )
    EmailAddress.objects.create(
        user=user,
        email="search@example.com",
        verified=True,
        primary=True,
    )
    return user


@pytest.fixture
def other_user(db):
    user = User.objects.create_user(
        username="otheruser",
        email="other@example.com",
        password="testpass123",
    )
    EmailAddress.objects.create(
        user=user,
        email="other@example.com",
        verified=True,
        primary=True,
    )
    return user


@pytest.fixture
def access_token(verified_user):
    refresh = RefreshToken.for_user(verified_user)
    return str(refresh.access_token)


@pytest.fixture
def other_access_token(other_user):
    refresh = RefreshToken.for_user(other_user)
    return str(refresh.access_token)


@pytest.fixture
def search_api_key(verified_user):
    """A search-scoped ApiKey for verified_user. Has a .raw_key attribute for use in headers."""
    raw_key, key_hash = generate_api_key()
    obj = ApiKey.objects.create(
        user=verified_user,
        key=key_hash,
        name="Test search key",
        scopes=[ApiKey.Scope.SEARCH],
    )
    obj.raw_key = raw_key
    return obj


@pytest.fixture
def crawl_api_key(verified_user):
    """A crawl-scoped ApiKey for verified_user. Has a .raw_key attribute for use in headers."""
    raw_key, key_hash = generate_api_key()
    obj = ApiKey.objects.create(
        user=verified_user,
        key=key_hash,
        name="Test crawl key",
        scopes=[ApiKey.Scope.CRAWL],
    )
    obj.raw_key = raw_key
    return obj


@pytest.fixture
def client(db):
    return Client()


def auth_headers(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def api_key_header(key):
    return {"HTTP_X_API_KEY": key}


# ---------------------------------------------------------------------------
# Helper: get the unified API client
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    """Return a Django test client pointed at the v1 API."""
    return Client()


# ---------------------------------------------------------------------------
# API key management — POST /api/v1/platform/api-keys/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_create_api_key(api_client, access_token):
    response = api_client.post(
        "/api/v1/platform/api-keys/",
        content_type="application/json",
        data={"name": "My app"},
        **auth_headers(access_token),
    )
    assert response.status_code == 200
    data = response.json()
    assert "key" in data
    assert data["name"] == "My app"
    assert data["scopes"] == ["search"]
    assert "id" in data
    assert "created_on" in data


@pytest.mark.django_db
def test_create_api_key_unauthenticated(api_client):
    response = api_client.post(
        "/api/v1/platform/api-keys/",
        content_type="application/json",
        data={"name": "My app"},
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_create_api_key_default_name(api_client, access_token):
    response = api_client.post(
        "/api/v1/platform/api-keys/",
        content_type="application/json",
        data={},
        **auth_headers(access_token),
    )
    assert response.status_code == 200
    assert response.json()["name"] == ""


# ---------------------------------------------------------------------------
# API key management — GET /api/v1/platform/api-keys/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_list_api_keys_hides_raw_key(api_client, access_token, search_api_key):
    response = api_client.get(
        "/api/v1/platform/api-keys/",
        **auth_headers(access_token),
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    for item in data:
        assert "key" not in item, "Raw key must not be exposed in list response"
        assert "id" in item
        assert "name" in item
        assert "scopes" in item


@pytest.mark.django_db
def test_list_api_keys_only_shows_search_scoped(api_client, access_token, search_api_key, crawl_api_key):
    response = api_client.get(
        "/api/v1/platform/api-keys/",
        **auth_headers(access_token),
    )
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert search_api_key.id in ids
    assert crawl_api_key.id not in ids


@pytest.mark.django_db
def test_list_api_keys_unauthenticated(api_client):
    response = api_client.get("/api/v1/platform/api-keys/")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# API key management — DELETE /api/v1/platform/api-keys/{id}
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_delete_api_key(api_client, access_token, search_api_key):
    key_id = search_api_key.id
    response = api_client.delete(
        f"/api/v1/platform/api-keys/{key_id}",
        **auth_headers(access_token),
    )
    assert response.status_code == 200
    assert not ApiKey.objects.filter(id=key_id).exists()


@pytest.mark.django_db
def test_delete_other_users_key_returns_404(api_client, other_access_token, search_api_key):
    """Attempting to delete another user's key returns 404 (not 403) to avoid leaking existence."""
    response = api_client.delete(
        f"/api/v1/platform/api-keys/{search_api_key.id}",
        **auth_headers(other_access_token),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_delete_nonexistent_key_returns_404(api_client, access_token):
    response = api_client.delete(
        "/api/v1/platform/api-keys/999999",
        **auth_headers(access_token),
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Search endpoint — authentication
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_search_missing_key_returns_results_with_null_usage(api_client):
    """Unauthenticated requests are allowed; usage fields are null."""
    response = api_client.get("/api/v2/search/?s=python")
    assert response.status_code == 200
    data = response.json()
    assert data["monthly_usage"] is None
    assert data["monthly_limit"] is None


@pytest.mark.django_db
def test_search_invalid_key(api_client):
    response = api_client.get("/api/v2/search/?s=python", **api_key_header("invalid-key"))
    assert response.status_code == 401


@pytest.mark.django_db
def test_search_crawl_scoped_key_rejected(api_client, crawl_api_key):
    """A crawl-scoped key must not grant access to the search endpoint."""
    response = api_client.get(
        "/api/v2/search/?s=python",
        **api_key_header(crawl_api_key.raw_key),
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Search endpoint — successful request with usage metadata
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_search_with_valid_key(api_client, search_api_key):
    with patch("mwmbl.tinysearchengine.search.check_rate_limit", return_value=True), \
         patch("mwmbl.tinysearchengine.search.get_monthly_count", return_value=0), \
         patch("mwmbl.tinysearchengine.search.increment_monthly", return_value=1):
        response = api_client.get(
            "/api/v2/search/?s=python",
            **api_key_header(search_api_key.raw_key),
        )
    assert response.status_code == 200


@pytest.mark.django_db
def test_search_response_includes_usage_fields(api_client, search_api_key):
    """When quota helpers are mocked, the response includes monthly_usage and monthly_limit."""
    with patch("mwmbl.tinysearchengine.search.check_rate_limit", return_value=True), \
         patch("mwmbl.tinysearchengine.search.get_monthly_count", return_value=5), \
         patch("mwmbl.tinysearchengine.search.increment_monthly", return_value=6), \
         patch("mwmbl.tinysearchengine.rank.HeuristicRanker.search", return_value=[]):
        response = api_client.get(
            "/api/v2/search/?s=python",
            **api_key_header(search_api_key.raw_key),
        )
    assert response.status_code == 200
    data = response.json()
    assert "monthly_usage" in data
    assert "monthly_limit" in data
    assert data["monthly_limit"] == MwmblUser.TIER_MONTHLY_LIMITS[MwmblUser.Tier.FREE]


# ---------------------------------------------------------------------------
# Search endpoint — quota enforcement
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_search_rate_limit_exceeded(api_client, search_api_key):
    with patch("mwmbl.tinysearchengine.search.check_rate_limit", return_value=False):
        response = api_client.get(
            "/api/v2/search/?s=python",
            **api_key_header(search_api_key.raw_key),
        )
    assert response.status_code == 429
    assert "rate limit" in response.json()["detail"].lower()


@pytest.mark.django_db
def test_search_monthly_quota_exceeded(api_client, search_api_key):
    limit = MwmblUser.TIER_MONTHLY_LIMITS[MwmblUser.Tier.FREE]
    with patch("mwmbl.tinysearchengine.search.check_rate_limit", return_value=True), \
         patch("mwmbl.tinysearchengine.search.get_monthly_count", return_value=limit):
        response = api_client.get(
            "/api/v2/search/?s=python",
            **api_key_header(search_api_key.raw_key),
        )
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert "quota" in detail.lower() or "monthly" in detail.lower()
    # Free tier should mention upgrade options
    assert "starter" in detail.lower() or "pro" in detail.lower() or "upgrade" in detail.lower()


@pytest.mark.django_db
def test_search_monthly_quota_exceeded_pro_no_upgrade_message(api_client, search_api_key, verified_user):
    """Pro tier 429 message should not suggest an upgrade."""
    verified_user.tier = MwmblUser.Tier.PRO
    verified_user.save()
    limit = MwmblUser.TIER_MONTHLY_LIMITS[MwmblUser.Tier.PRO]
    with patch("mwmbl.tinysearchengine.search.check_rate_limit", return_value=True), \
         patch("mwmbl.tinysearchengine.search.get_monthly_count", return_value=limit):
        response = api_client.get(
            "/api/v2/search/?s=python",
            **api_key_header(search_api_key.raw_key),
        )
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert "upgrade" not in detail.lower()


# ---------------------------------------------------------------------------
# Crawler /results — header vs body key and scope enforcement
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_post_results_no_key_returns_401(api_client):
    response = api_client.post(
        "/api/v1/crawler/results",
        content_type="application/json",
        data={"results": []},
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_post_results_header_key_accepted(api_client, crawl_api_key):
    with patch("mwmbl.crawler.app.index_documents"), \
         patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"), \
         patch("mwmbl.crawler.app.stats_manager"):
        response = api_client.post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"results": []},
            **api_key_header(crawl_api_key.raw_key),
        )
    assert response.status_code == 200


@pytest.mark.django_db
def test_post_results_body_key_deprecated_still_works(api_client, crawl_api_key):
    """Body api_key field is deprecated but must still work for backward compatibility."""
    with patch("mwmbl.crawler.app.index_documents"), \
         patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"), \
         patch("mwmbl.crawler.app.stats_manager"):
        response = api_client.post(
            "/api/v1/crawler/results",
            content_type="application/json",
            data={"api_key": crawl_api_key.raw_key, "results": []},
        )
    assert response.status_code == 200


@pytest.mark.django_db
def test_post_results_search_scoped_key_rejected(api_client, search_api_key):
    """A search-scoped key must not grant access to the crawler results endpoint."""
    response = api_client.post(
        "/api/v1/crawler/results",
        content_type="application/json",
        data={"results": []},
        **api_key_header(search_api_key.raw_key),
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_post_results_header_takes_precedence_over_body(api_client, crawl_api_key, search_api_key):
    """When both header and body key are present, the header key is used."""
    with patch("mwmbl.crawler.app.index_documents"), \
         patch("mwmbl.crawler.app.upload_object", return_value="fake/path.json.gz"), \
         patch("mwmbl.crawler.app.stats_manager"):
        response = api_client.post(
            "/api/v1/crawler/results",
            content_type="application/json",
            # Header has valid crawl key; body has search key (wrong scope)
            data={"api_key": search_api_key.raw_key, "results": []},
            **api_key_header(crawl_api_key.raw_key),
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_sync_search_counts_redis_to_postgres(verified_user):
    """sync_search_counts should write live Redis counts into UsageBucket."""

    now = datetime.utcnow()
    key = _monthly_key(verified_user.id)
    cache.set(key, 42, timeout=3600)

    with patch("mwmbl.background.get_all_monthly_keys", return_value=[key]):
        sync_search_counts.now()

    bucket = UsageBucket.objects.get(user=verified_user, year=now.year, month=now.month)
    assert bucket.count == 42

    cache.delete(key)


@pytest.mark.django_db
def test_sync_search_counts_seeds_redis_from_postgres(verified_user):
    """sync_search_counts should restore missing Redis keys from UsageBucket (Redis restart recovery)."""

    now = datetime.utcnow()
    key = _monthly_key(verified_user.id)

    # Simulate Redis restart: bucket exists in Postgres but key is absent from Redis
    UsageBucket.objects.create(user=verified_user, year=now.year, month=now.month, count=99)
    cache.delete(key)

    with patch("mwmbl.background.get_all_monthly_keys", return_value=[]):
        sync_search_counts.now()

    assert get_monthly_count(verified_user.id) == 99

    cache.delete(key)


@pytest.mark.django_db
def test_sync_search_counts_uses_postgres_value_when_higher(verified_user):
    """After a Redis restart, requests may have incremented from zero before the sync runs.
    The sync should take the max so the Postgres baseline is not lost."""

    now = datetime.utcnow()
    key = _monthly_key(verified_user.id)

    # Postgres has the pre-restart count; Redis has only post-restart requests
    UsageBucket.objects.create(user=verified_user, year=now.year, month=now.month, count=70)
    cache.set(key, 5, timeout=3600)

    with patch("mwmbl.background.get_all_monthly_keys", return_value=[]):
        sync_search_counts.now()

    assert get_monthly_count(verified_user.id) == 70

    cache.delete(key)


@pytest.mark.django_db
def test_sync_search_counts_keeps_redis_value_when_higher(verified_user):
    """If Redis is ahead of Postgres (normal operation), the Redis value is kept."""

    now = datetime.utcnow()
    key = _monthly_key(verified_user.id)

    # Redis is ahead because requests arrived since the last sync
    UsageBucket.objects.create(user=verified_user, year=now.year, month=now.month, count=70)
    cache.set(key, 85, timeout=3600)

    with patch("mwmbl.background.get_all_monthly_keys", return_value=[]):
        sync_search_counts.now()

    assert get_monthly_count(verified_user.id) == 85

    cache.delete(key)


# ---------------------------------------------------------------------------
# Subscription endpoint — GET /api/v1/platform/billing/subscription
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_subscription_unauthenticated(api_client):
    response = api_client.get("/api/v1/platform/billing/subscription")
    assert response.status_code == 401


@pytest.mark.django_db
def test_subscription_free_user_no_usage(api_client, access_token, verified_user):
    response = api_client.get(
        "/api/v1/platform/billing/subscription",
        **auth_headers(access_token),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["plan"] == MwmblUser.Tier.FREE
    assert data["status"] == "free"
    assert data["monthly_limit"] == MwmblUser.TIER_MONTHLY_LIMITS[MwmblUser.Tier.FREE]
    assert data["monthly_usage"] == 0
    assert data["current_period_end"] is None
    assert data["polar_customer_id"] is None


@pytest.mark.django_db
def test_subscription_reflects_live_redis_count(api_client, access_token, verified_user):
    """Subscription usage comes from Redis (live count), not the periodic Postgres sync."""
    key = _monthly_key(verified_user.id)
    cache.set(key, 42, timeout=3600)

    response = api_client.get(
        "/api/v1/platform/billing/subscription",
        **auth_headers(access_token),
    )
    assert response.status_code == 200
    assert response.json()["monthly_usage"] == 42

    cache.delete(key)


@pytest.mark.django_db
def test_subscription_usage_matches_search_response(api_client, access_token, verified_user):
    """Subscription usage equals the live Redis count incremented by each search request."""

    raw_key, key_hash = generate_api_key()
    ApiKey.objects.create(
        user=verified_user,
        key=key_hash,
        name="Test key",
        scopes=[ApiKey.Scope.SEARCH],
    )
    cache.delete(_monthly_key(verified_user.id))

    search_resp = api_client.get("/api/v2/search/?s=python", **api_key_header(raw_key))
    assert search_resp.status_code == 200
    search_usage = search_resp.json()["monthly_usage"]
    assert search_usage == 1

    sub_resp = api_client.get(
        "/api/v1/platform/billing/subscription",
        **auth_headers(access_token),
    )
    assert sub_resp.status_code == 200
    assert sub_resp.json()["monthly_usage"] == search_usage

    cache.delete(_monthly_key(verified_user.id))


@pytest.mark.django_db
def test_subscription_correct_limit_for_starter_tier(api_client, access_token, verified_user):
    verified_user.tier = MwmblUser.Tier.STARTER
    verified_user.save()

    response = api_client.get(
        "/api/v1/platform/billing/subscription",
        **auth_headers(access_token),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["plan"] == MwmblUser.Tier.STARTER
    assert data["status"] == "active"
    assert data["monthly_limit"] == MwmblUser.TIER_MONTHLY_LIMITS[MwmblUser.Tier.STARTER]


# ---------------------------------------------------------------------------
# Quota helper unit tests
# ---------------------------------------------------------------------------

def test_rate_limit_allows_up_to_limit():

    user_id = 99999
    rate_key = f"search:rate:{user_id}"
    cache.delete(rate_key)

    results = [check_rate_limit(user_id) for _ in range(RATE_LIMIT)]
    assert all(results), "All requests within limit should be allowed"

    # The next one should be rejected
    assert not check_rate_limit(user_id), "Request exceeding rate limit should be rejected"

    cache.delete(rate_key)


def test_increment_monthly_returns_increasing_counts():

    user_id = 88888
    key = _monthly_key(user_id)
    cache.delete(key)

    assert increment_monthly(user_id) == 1
    assert increment_monthly(user_id) == 2
    assert increment_monthly(user_id) == 3

    cache.delete(key)
