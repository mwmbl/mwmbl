"""Tests for the paying users admin page (mwmbl.admin_views.paying_users_view)."""

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from mwmbl import quota
from mwmbl.models import Membership, UserBilling
from mwmbl.templatetags.money import minor_units

User = get_user_model()

PAYING_USERS_URL = "/admin/paying-users/"


@pytest.fixture(autouse=True)
def clear_cache():
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
    assert b"No API subscribers." in response.content
    assert b"No members." in response.content


def test_api_customer_quota_usage_and_cost(staff_client):
    customer = User.objects.create_user(username="api_customer", email="api@example.com")
    UserBilling.objects.create(
        user=customer, polar_customer_id="cus_1", polar_subscription_id="sub_1", max_monthly_spend_cents=1_000
    )
    for _ in range(3_000):
        quota.increment_monthly(customer.id)
    # Never subscribed, so not a customer at all.
    UserBilling.objects.create(user=User.objects.create_user(username="never_paid"))

    response = staff_client.get(PAYING_USERS_URL)

    [row] = response.context["api_customers"]
    assert row["user"] == customer
    assert row["status"] == "active"
    assert row["monthly_cap"] == 4_000
    assert row["usage"] == 3_000
    assert row["estimated_cost_cents"] == 500
    assert response.context["api_billed_count"] == 1
    assert response.context["api_estimated_revenue_cents"] == 500
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


def test_minor_units():
    assert minor_units(0, "$") == "$0.00"
    assert minor_units(123_456, "£") == "£1,234.56"
