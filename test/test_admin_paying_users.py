"""Tests for the paying users admin page (mwmbl.admin_views.paying_users_view)."""

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from fakeredis import FakeConnection

from mwmbl import quota
from mwmbl.models import ApiKey, Membership, UserBilling
from mwmbl.templatetags.money import minor_units

User = get_user_model()

PAYING_USERS_URL = "/admin/paying-users/"


@pytest.fixture(autouse=True)
def redis_cache(settings):
    """django-redis over fakeredis rather than the test settings' locmem cache, because the
    Seed Search stats scan the keyspace, which only django-redis supports."""
    settings.CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": "redis://localhost:6379/0",
            "OPTIONS": {"CONNECTION_POOL_KWARGS": {"connection_class": FakeConnection}},
        }
    }
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user(username="staff_member_1", password="correctpassword", is_staff=True)
    client.force_login(user)
    return client


def test_requires_staff(client, db):
    user = User.objects.create_user(username="not_staff", password="correctpassword")
    client.force_login(user)

    response = client.get(PAYING_USERS_URL)

    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


def test_empty_page_renders(staff_client):
    response = staff_client.get(PAYING_USERS_URL)

    assert response.status_code == 200
    assert b"No API users." in response.content
    assert b"No members." in response.content


def test_api_subscriber_quota_usage_and_cost(staff_client):
    customer = User.objects.create_user(username="api_customer", email="api@example.com")
    UserBilling.objects.create(
        user=customer, polar_customer_id="cus_1", polar_subscription_id="sub_1", max_monthly_spend_cents=1_000
    )
    for _ in range(3_000):
        quota.increment_monthly(customer.id)

    response = staff_client.get(PAYING_USERS_URL)

    [row] = response.context["api_users"]
    assert row["user"] == customer
    assert row["status"] == "active"
    assert row["monthly_cap"] == 4_000
    assert row["usage"] == 3_000
    assert row["estimated_cost_cents"] == 500
    assert response.context["api_subscribed_count"] == 1
    assert response.context["api_billed_count"] == 1
    assert response.context["api_estimated_revenue_cents"] == 500


def test_search_key_holders_without_a_subscription_are_listed(staff_client):
    key_holder = User.objects.create_user(username="key_holder")
    ApiKey.objects.create(user=key_holder, key="hash_1", scopes=[ApiKey.Scope.SEARCH])
    ApiKey.objects.create(user=key_holder, key="hash_2", scopes=[ApiKey.Scope.SEARCH, ApiKey.Scope.CRAWL])
    crawler = User.objects.create_user(username="crawler_only")
    ApiKey.objects.create(user=crawler, key="hash_3", scopes=[ApiKey.Scope.CRAWL])
    # A billing row with no subscription, as a spend-limit request without checkout leaves.
    UserBilling.objects.create(user=User.objects.create_user(username="never_paid"))
    quota.increment_monthly(key_holder.id)

    response = staff_client.get(PAYING_USERS_URL)

    [row] = response.context["api_users"]
    assert row["user"] == key_holder
    assert row["status"] == "no subscription"
    assert row["monthly_cap"] == 2_000
    assert row["usage"] == 1
    assert row["estimated_cost_cents"] == 0
    assert response.context["api_subscribed_count"] == 0
    assert b"crawler_only" not in response.content
    assert b"never_paid" not in response.content


def test_members_quota_usage_and_revenue(staff_client):
    sapling = User.objects.create_user(username="sapling_member")
    Membership.objects.create(user=sapling, tier="sapling", polar_subscription_id="sub_sapling")
    canopy = User.objects.create_user(username="canopy_member")
    Membership.objects.create(user=canopy, tier="canopy", polar_subscription_id="sub_canopy", cancel_at_period_end=True)
    quota.increment_monthly_combined_search(sapling.id)

    response = staff_client.get(PAYING_USERS_URL)

    members = {member["user"].username: member for member in response.context["members"]}
    assert members["sapling_member"]["combined_search_limit"] == 1_500
    assert members["sapling_member"]["combined_search_usage"] == 1
    assert members["sapling_member"]["monthly_price_pence"] == 500
    assert members["canopy_member"]["combined_search_usage"] == 0
    counts = {tier["tier"]: tier["count"] for tier in response.context["tier_summary"]}
    assert counts == {"Sprout": 0, "Sapling": 1, "Canopy": 1}
    assert response.context["membership_revenue_pence"] == 2_500
    assert "£25.00".encode() in response.content


def test_seed_search_usage_by_tier(staff_client):
    free_user = User.objects.create_user(username="free_user")
    sprout = User.objects.create_user(username="sprout_member")
    Membership.objects.create(user=sprout, tier="sprout", polar_subscription_id="sub_sprout")
    for _ in range(30):
        quota.increment_monthly_combined_search(free_user.id)
    for _ in range(5):
        quota.increment_monthly_combined_search(sprout.id)
    # Standard search usage is a different counter and must not be counted.
    quota.increment_monthly(sprout.id)

    response = staff_client.get(PAYING_USERS_URL)

    seed_search = response.context["seed_search"]
    assert seed_search["users"] == 2
    assert seed_search["queries"] == 35
    assert seed_search["at_limit"] == 1
    by_tier = {tier["tier"]: tier for tier in seed_search["by_tier"]}
    assert by_tier["free"] == {"tier": "free", "users": 1, "queries": 30, "at_limit": 1, "limit": 30}
    assert by_tier["sprout"] == {"tier": "sprout", "users": 1, "queries": 5, "at_limit": 0, "limit": 300}
    assert by_tier["canopy"]["users"] == 0
    assert [row["user"].username for row in seed_search["top_users"]] == ["free_user", "sprout_member"]


def test_seed_search_skips_deleted_users(staff_client):
    deleted = User.objects.create_user(username="deleted_user")
    quota.increment_monthly_combined_search(deleted.id)
    deleted.delete()

    response = staff_client.get(PAYING_USERS_URL)

    assert response.status_code == 200
    seed_search = response.context["seed_search"]
    assert seed_search["users"] == 0
    assert seed_search["top_users"] == []


def test_breadcrumbs_do_not_contain_seed_search_tables(staff_client):
    response = staff_client.get(PAYING_USERS_URL)

    assert response.content.count(b"Seed Search this month") == 1


def test_minor_units():
    assert minor_units(0, "$") == "$0.00"
    assert minor_units(123_456, "£") == "£1,234.56"
