"""
Tests for supporter membership endpoints.

Covers:
- GET /api/v1/platform/membership/tiers
- GET /api/v1/platform/membership
- POST /api/v1/platform/membership/checkout
- Membership subscription events on POST /api/v1/platform/billing/webhook
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from ninja_jwt.tokens import RefreshToken

from mwmbl.membership import MembershipTier
from mwmbl.models import Membership, UserBilling

User = get_user_model()

PRODUCT_SETTINGS = override_settings(
    POLAR_PRODUCT_ID_SEED="prod_seed",
    POLAR_PRODUCT_ID_SAPLING="prod_sapling",
    POLAR_PRODUCT_ID_CANOPY="prod_canopy",
)


@pytest.fixture
def user(db):
    user = User.objects.create_user(username="memberuser", email="member@example.com", password="testpass123")
    EmailAddress.objects.create(user=user, email="member@example.com", verified=True, primary=True)
    return user


@pytest.fixture
def api_client(db):
    return Client()


def auth_headers(user):
    token = RefreshToken.for_user(user).access_token
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _membership_event(event_type, user_id, product_id="prod_sapling", status="active", **data_overrides):
    event = Mock()
    event.TYPE = event_type
    event.data = Mock()
    event.data.metadata = {"user_id": str(user_id)}
    event.data.product_id = product_id
    event.data.status = status
    event.data.id = data_overrides.get("subscription_id", "sub_member")
    event.data.customer_id = "cust_member"
    event.data.current_period_end = data_overrides.get("current_period_end", None)
    event.data.cancel_at_period_end = data_overrides.get("cancel_at_period_end", False)
    return event


def _post_webhook(api_client, event):
    with patch("mwmbl.platform.api.validate_event", return_value=event):
        return api_client.post("/api/v1/platform/billing/webhook", content_type="application/json", data={})


# ---------------------------------------------------------------------------
# Tiers and current membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_list_tiers_is_public_and_ordered(api_client):
    response = api_client.get("/api/v1/platform/membership/tiers")

    assert response.status_code == 200
    tiers = response.json()
    assert [(t["tier"], t["monthly_price_pence"]) for t in tiers] == [
        ("seed", 100),
        ("sapling", 500),
        ("canopy", 2_000),
    ]
    assert tiers[1]["name"] == "Sapling"


@pytest.mark.django_db
def test_get_membership_not_a_member_returns_404(api_client, user):
    response = api_client.get("/api/v1/platform/membership", **auth_headers(user))

    assert response.status_code == 404


@pytest.mark.django_db
def test_get_membership_returns_tier(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_1")

    response = api_client.get("/api/v1/platform/membership", **auth_headers(user))

    assert response.status_code == 200
    assert response.json()["tier"] == "canopy"
    assert response.json()["cancel_at_period_end"] is False


@pytest.mark.django_db
def test_get_membership_unauthenticated(api_client):
    response = api_client.get("/api/v1/platform/membership")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_checkout_uses_tier_product(api_client, user):
    with patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar = MockPolar.return_value.__enter__.return_value
        mock_polar.checkouts.create.return_value.url = "https://polar.example/checkout/member"

        response = api_client.post(
            "/api/v1/platform/membership/checkout",
            content_type="application/json",
            data={"tier": "sapling", "success_url": "https://mwmbl.org/welcome"},
            **auth_headers(user),
        )

    assert response.status_code == 200
    assert response.json()["checkout_url"] == "https://polar.example/checkout/member"
    checkout_params = mock_polar.checkouts.create.call_args[1]["request"]
    assert checkout_params["products"] == ["prod_sapling"]
    assert checkout_params["metadata"] == {"user_id": str(user.id)}
    assert checkout_params["success_url"] == "https://mwmbl.org/welcome"


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_checkout_rejects_existing_member(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SEED, polar_subscription_id="sub_1")

    response = api_client.post(
        "/api/v1/platform/membership/checkout",
        content_type="application/json",
        data={"tier": "canopy"},
        **auth_headers(user),
    )

    assert response.status_code == 409


@pytest.mark.django_db
@override_settings(POLAR_PRODUCT_ID_SAPLING="")
def test_checkout_not_configured_returns_503(api_client, user):
    response = api_client.post(
        "/api/v1/platform/membership/checkout",
        content_type="application/json",
        data={"tier": "sapling"},
        **auth_headers(user),
    )

    assert response.status_code == 503


@pytest.mark.django_db
def test_checkout_unknown_tier_is_rejected(api_client, user):
    response = api_client.post(
        "/api/v1/platform/membership/checkout",
        content_type="application/json",
        data={"tier": "oak"},
        **auth_headers(user),
    )

    assert response.status_code == 422


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_checkout_requires_verified_email(api_client, user):
    EmailAddress.objects.filter(user=user).update(verified=False)

    response = api_client.post(
        "/api/v1/platform/membership/checkout",
        content_type="application/json",
        data={"tier": "seed"},
        **auth_headers(user),
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_active_creates_membership(api_client, user):
    period_end = datetime(2026, 11, 1, tzinfo=timezone.utc)
    event = _membership_event("subscription.active", user.id, current_period_end=period_end)

    response = _post_webhook(api_client, event)

    assert response.status_code == 200
    member = Membership.objects.get(user=user)
    assert member.tier == MembershipTier.SAPLING
    assert member.polar_subscription_id == "sub_member"
    assert member.current_period_end == period_end


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_does_not_touch_usage_billing(api_client, user):
    UserBilling.objects.create(user=user, polar_subscription_id="sub_usage", max_monthly_spend_cents=1_000)

    _post_webhook(api_client, _membership_event("subscription.active", user.id))
    _post_webhook(api_client, _membership_event("subscription.revoked", user.id, status="canceled"))

    billing = UserBilling.objects.get(user=user)
    assert billing.polar_subscription_id == "sub_usage"
    assert billing.max_monthly_spend_cents == 1_000


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_updated_changes_tier(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SEED, polar_subscription_id="sub_member")

    _post_webhook(api_client, _membership_event("subscription.updated", user.id, product_id="prod_canopy"))

    assert Membership.objects.get(user=user).tier == MembershipTier.CANOPY


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_scheduled_cancel_keeps_membership(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    _post_webhook(api_client, _membership_event("subscription.canceled", user.id, cancel_at_period_end=True))

    assert Membership.objects.get(user=user).cancel_at_period_end is True


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_revoked_ends_membership(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    _post_webhook(api_client, _membership_event("subscription.revoked", user.id, status="canceled"))

    assert not Membership.objects.filter(user=user).exists()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_late_event_for_old_subscription_keeps_newer_membership(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_new")

    _post_webhook(
        api_client, _membership_event("subscription.revoked", user.id, status="canceled", subscription_id="sub_old")
    )

    assert Membership.objects.get(user=user).polar_subscription_id == "sub_new"
