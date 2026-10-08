"""
Tests for supporter membership endpoints.

Covers:
- GET /api/v1/platform/membership/tiers
- GET /api/v1/platform/membership
- POST /api/v1/platform/membership/checkout
- POST /api/v1/platform/membership/cancel and /uncancel
- POST /api/v1/platform/membership/change
- Membership subscription events on POST /api/v1/platform/billing/webhook
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from ninja_jwt.tokens import RefreshToken
from polar_sdk.models import AlreadyCanceledSubscription, SubscriptionLocked

from mwmbl.membership import MembershipTier
from mwmbl.models import Membership, UserBilling

User = get_user_model()

PRODUCT_SETTINGS = override_settings(
    POLAR_PRODUCT_ID_USAGE="prod_usage",
    POLAR_PRODUCT_ID_SPROUT="prod_sprout",
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


def _polar_subscription(
    subscription_id="sub_member",
    product_id="prod_sapling",
    created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    current_period_end=None,
    cancel_at_period_end=False,
):
    subscription = Mock()
    subscription.id = subscription_id
    subscription.product_id = product_id
    subscription.created_at = created_at
    subscription.current_period_end = current_period_end
    subscription.cancel_at_period_end = cancel_at_period_end
    return subscription


def _mock_polar(MockPolar, live_subscriptions):
    """Make Polar report these live membership subscriptions for the user."""
    mock_polar = MockPolar.return_value.__enter__.return_value
    mock_polar.subscriptions.list.return_value.result.items = live_subscriptions
    return mock_polar


def _post_webhook(api_client, event, live_subscriptions):
    with (
        patch("mwmbl.platform.api.validate_event", return_value=event),
        patch("mwmbl.platform.api.Polar") as MockPolar,
    ):
        mock_polar = _mock_polar(MockPolar, live_subscriptions)
        response = api_client.post("/api/v1/platform/billing/webhook", content_type="application/json", data={})
    return response, mock_polar


# ---------------------------------------------------------------------------
# Tiers and current membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_list_tiers_is_public_and_ordered(api_client):
    response = api_client.get("/api/v1/platform/membership/tiers")

    assert response.status_code == 200
    tiers = response.json()
    assert [(t["tier"], t["monthly_price_pence"]) for t in tiers] == [
        ("sprout", 100),
        ("sapling", 500),
        ("canopy", 2_000),
    ]
    assert tiers[1]["name"] == "Sapling"


@pytest.mark.django_db
def test_tier_perks_contain_the_phrases_the_front_end_bolds(api_client):
    response = api_client.get("/api/v1/platform/membership/tiers")

    perks_by_tier = {t["tier"]: " ".join(t["perks"]) for t in response.json()}
    assert "300 Seed Search queries" in perks_by_tier["sprout"]
    assert "1,500 Seed Search queries" in perks_by_tier["sapling"]
    assert "1 million pages" in perks_by_tier["canopy"]


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
        mock_polar = _mock_polar(MockPolar, [])
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
    Membership.objects.create(user=user, tier=MembershipTier.SPROUT, polar_subscription_id="sub_1")

    response = api_client.post(
        "/api/v1/platform/membership/checkout",
        content_type="application/json",
        data={"tier": "canopy"},
        **auth_headers(user),
    )

    assert response.status_code == 409


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_checkout_rejects_subscription_paid_before_its_webhook(api_client, user):
    with patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar = _mock_polar(MockPolar, [_polar_subscription(product_id="prod_sprout")])

        response = api_client.post(
            "/api/v1/platform/membership/checkout",
            content_type="application/json",
            data={"tier": "canopy"},
            **auth_headers(user),
        )

    assert response.status_code == 409
    list_params = mock_polar.subscriptions.list.call_args[1]
    assert list_params["external_customer_id"] == str(user.id)
    assert sorted(list_params["product_id"]) == ["prod_canopy", "prod_sapling", "prod_sprout"]
    mock_polar.checkouts.create.assert_not_called()


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
        data={"tier": "sprout"},
        **auth_headers(user),
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Cancel and uncancel
# ---------------------------------------------------------------------------


def _post_membership_action(api_client, user, action, polar_update_result=None, polar_update_error=None):
    with patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar = MockPolar.return_value.__enter__.return_value
        mock_polar.subscriptions.update.return_value = polar_update_result
        mock_polar.subscriptions.update.side_effect = polar_update_error
        response = api_client.post(f"/api/v1/platform/membership/{action}", **auth_headers(user))
    return response, mock_polar


def _already_canceled_error():
    raw_response = Mock(status_code=403, text="Subscription is already canceled")
    return AlreadyCanceledSubscription(data=Mock(detail="Subscription is already canceled"), raw_response=raw_response)


@pytest.mark.django_db
def test_cancel_membership_schedules_cancellation(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")
    period_end = datetime(2026, 11, 1, tzinfo=timezone.utc)

    response, mock_polar = _post_membership_action(
        api_client, user, "cancel", polar_update_result=_polar_subscription(current_period_end=period_end)
    )

    assert response.status_code == 200
    assert response.json()["cancel_at_period_end"] is True
    assert response.json()["tier"] == "sapling"
    update_params = mock_polar.subscriptions.update.call_args[1]
    assert update_params["id"] == "sub_member"
    assert update_params["subscription_update"].cancel_at_period_end is True
    member = Membership.objects.get(user=user)
    assert member.cancel_at_period_end is True
    assert member.current_period_end == period_end


@pytest.mark.django_db
def test_cancel_membership_not_a_member_returns_404(api_client, user):
    response, mock_polar = _post_membership_action(api_client, user, "cancel")

    assert response.status_code == 404
    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
def test_cancel_membership_already_cancelling_returns_409(api_client, user):
    Membership.objects.create(
        user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member", cancel_at_period_end=True
    )

    response, mock_polar = _post_membership_action(api_client, user, "cancel")

    assert response.status_code == 409
    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
def test_cancel_membership_already_canceled_in_polar_returns_409(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    response, _ = _post_membership_action(api_client, user, "cancel", polar_update_error=_already_canceled_error())

    assert response.status_code == 409
    assert Membership.objects.get(user=user).cancel_at_period_end is False


@pytest.mark.django_db
def test_cancel_membership_requires_verified_email(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")
    EmailAddress.objects.filter(user=user).update(verified=False)

    response, _ = _post_membership_action(api_client, user, "cancel")

    assert response.status_code == 403


@pytest.mark.django_db
def test_cancel_membership_unauthenticated(api_client):
    response = api_client.post("/api/v1/platform/membership/cancel")

    assert response.status_code == 401


@pytest.mark.django_db
def test_uncancel_membership_removes_scheduled_cancellation(api_client, user):
    Membership.objects.create(
        user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_member", cancel_at_period_end=True
    )
    period_end = datetime(2026, 11, 1, tzinfo=timezone.utc)

    response, mock_polar = _post_membership_action(
        api_client, user, "uncancel", polar_update_result=_polar_subscription(current_period_end=period_end)
    )

    assert response.status_code == 200
    assert response.json()["cancel_at_period_end"] is False
    update_params = mock_polar.subscriptions.update.call_args[1]
    assert update_params["id"] == "sub_member"
    assert update_params["subscription_update"].cancel_at_period_end is False
    member = Membership.objects.get(user=user)
    assert member.cancel_at_period_end is False
    assert member.current_period_end == period_end


@pytest.mark.django_db
def test_uncancel_membership_not_a_member_returns_404(api_client, user):
    response, _ = _post_membership_action(api_client, user, "uncancel")

    assert response.status_code == 404


@pytest.mark.django_db
def test_uncancel_membership_not_cancelling_returns_409(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_member")

    response, mock_polar = _post_membership_action(api_client, user, "uncancel")

    assert response.status_code == 409
    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
def test_uncancel_membership_already_ended_in_polar_returns_409(api_client, user):
    Membership.objects.create(
        user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_member", cancel_at_period_end=True
    )

    response, _ = _post_membership_action(api_client, user, "uncancel", polar_update_error=_already_canceled_error())

    assert response.status_code == 409
    assert Membership.objects.get(user=user).cancel_at_period_end is True


@pytest.mark.django_db
def test_uncancel_membership_does_not_recreate_a_membership_deleted_during_the_polar_call(api_client, user):
    Membership.objects.create(
        user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_member", cancel_at_period_end=True
    )

    def revoke_webhook_arrives(**kwargs):
        Membership.objects.filter(user=user).delete()
        return _polar_subscription()

    response, _ = _post_membership_action(api_client, user, "uncancel", polar_update_error=revoke_webhook_arrives)

    assert response.status_code == 404
    assert not Membership.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_cancel_membership_does_not_overwrite_a_subscription_changed_during_the_polar_call(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    def upgrade_webhook_arrives(**kwargs):
        Membership.objects.filter(user=user).update(tier=MembershipTier.CANOPY, polar_subscription_id="sub_new")
        return _polar_subscription()

    response, _ = _post_membership_action(api_client, user, "cancel", polar_update_error=upgrade_webhook_arrives)

    assert response.status_code == 200
    assert response.json()["tier"] == "canopy"
    member = Membership.objects.get(user=user)
    assert member.polar_subscription_id == "sub_new"
    assert member.cancel_at_period_end is False


# ---------------------------------------------------------------------------
# Change tier
# ---------------------------------------------------------------------------


def _post_change(api_client, user, tier, polar_update_result=None, polar_update_error=None):
    with patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar = MockPolar.return_value.__enter__.return_value
        mock_polar.subscriptions.update.return_value = polar_update_result
        mock_polar.subscriptions.update.side_effect = polar_update_error
        response = api_client.post(
            "/api/v1/platform/membership/change",
            data={"tier": tier},
            content_type="application/json",
            **auth_headers(user),
        )
    return response, mock_polar


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_switches_product_with_proration(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")
    period_end = datetime(2026, 11, 1, tzinfo=timezone.utc)

    response, mock_polar = _post_change(
        api_client,
        user,
        "canopy",
        polar_update_result=_polar_subscription(product_id="prod_canopy", current_period_end=period_end),
    )

    assert response.status_code == 200
    assert response.json()["tier"] == "canopy"
    update_params = mock_polar.subscriptions.update.call_args[1]
    assert update_params["id"] == "sub_member"
    assert update_params["subscription_update"].product_id == "prod_canopy"
    assert update_params["subscription_update"].proration_behavior == "prorate"
    member = Membership.objects.get(user=user)
    assert member.tier == MembershipTier.CANOPY
    assert member.current_period_end == period_end


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_not_a_member_returns_404(api_client, user):
    response, mock_polar = _post_change(api_client, user, "canopy")

    assert response.status_code == 404
    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_to_current_tier_returns_409(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    response, mock_polar = _post_change(api_client, user, "sapling")

    assert response.status_code == 409
    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_while_cancelling_returns_409(api_client, user):
    Membership.objects.create(
        user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member", cancel_at_period_end=True
    )

    response, mock_polar = _post_change(api_client, user, "sprout")

    assert response.status_code == 409
    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_already_canceled_in_polar_returns_409(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    response, _ = _post_change(api_client, user, "canopy", polar_update_error=_already_canceled_error())

    assert response.status_code == 409
    assert Membership.objects.get(user=user).tier == MembershipTier.SAPLING


@pytest.mark.django_db
@override_settings(POLAR_PRODUCT_ID_CANOPY="")
def test_change_membership_not_configured_returns_503(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    response, mock_polar = _post_change(api_client, user, "canopy")

    assert response.status_code == 503
    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_requires_verified_email(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")
    EmailAddress.objects.filter(user=user).update(verified=False)

    response, _ = _post_change(api_client, user, "canopy")

    assert response.status_code == 403


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_leaves_cancel_flag_alone(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    response, _ = _post_change(
        api_client, user, "sprout", polar_update_result=_polar_subscription(product_id="prod_sprout")
    )

    assert response.status_code == 200
    assert response.json()["cancel_at_period_end"] is False
    assert Membership.objects.get(user=user).cancel_at_period_end is False


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_locked_in_polar_returns_409(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")
    raw_response = Mock(status_code=409, text="Subscription is locked")
    locked_error = SubscriptionLocked(data=Mock(detail="Subscription is locked"), raw_response=raw_response)

    response, _ = _post_change(api_client, user, "canopy", polar_update_error=locked_error)

    assert response.status_code == 409
    assert Membership.objects.get(user=user).tier == MembershipTier.SAPLING


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_unknown_tier_is_rejected(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    response, mock_polar = _post_change(api_client, user, "redwood")

    assert response.status_code == 422
    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_does_not_recreate_a_membership_deleted_during_the_polar_call(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    def revoke_webhook_arrives(**kwargs):
        Membership.objects.filter(user=user).delete()
        return _polar_subscription(product_id="prod_canopy")

    response, _ = _post_change(api_client, user, "canopy", polar_update_error=revoke_webhook_arrives)

    assert response.status_code == 404
    assert not Membership.objects.filter(user=user).exists()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_change_membership_does_not_overwrite_a_subscription_changed_during_the_polar_call(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    def new_subscription_webhook_arrives(**kwargs):
        Membership.objects.filter(user=user).update(tier=MembershipTier.SPROUT, polar_subscription_id="sub_new")
        return _polar_subscription(product_id="prod_canopy")

    response, _ = _post_change(api_client, user, "canopy", polar_update_error=new_subscription_webhook_arrives)

    assert response.status_code == 200
    assert response.json()["tier"] == "sprout"
    member = Membership.objects.get(user=user)
    assert member.polar_subscription_id == "sub_new"
    assert member.tier == MembershipTier.SPROUT


@pytest.mark.django_db
def test_change_membership_unauthenticated(api_client):
    response = api_client.post(
        "/api/v1/platform/membership/change", data={"tier": "canopy"}, content_type="application/json"
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_active_creates_membership(api_client, user):
    period_end = datetime(2026, 11, 1, tzinfo=timezone.utc)
    event = _membership_event("subscription.active", user.id)

    response, _ = _post_webhook(api_client, event, [_polar_subscription(current_period_end=period_end)])

    assert response.status_code == 200
    member = Membership.objects.get(user=user)
    assert member.tier == MembershipTier.SAPLING
    assert member.polar_subscription_id == "sub_member"
    assert member.current_period_end == period_end


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_does_not_touch_usage_billing(api_client, user):
    UserBilling.objects.create(user=user, polar_subscription_id="sub_usage", max_monthly_spend_cents=1_000)

    _post_webhook(api_client, _membership_event("subscription.active", user.id), [_polar_subscription()])
    _post_webhook(api_client, _membership_event("subscription.revoked", user.id, status="canceled"), [])

    billing = UserBilling.objects.get(user=user)
    assert billing.polar_subscription_id == "sub_usage"
    assert billing.max_monthly_spend_cents == 1_000


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_updated_changes_tier(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SPROUT, polar_subscription_id="sub_member")
    event = _membership_event("subscription.updated", user.id, product_id="prod_canopy")

    _post_webhook(api_client, event, [_polar_subscription(product_id="prod_canopy")])

    assert Membership.objects.get(user=user).tier == MembershipTier.CANOPY


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_scheduled_cancel_keeps_membership(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")
    event = _membership_event("subscription.canceled", user.id, cancel_at_period_end=True)

    _post_webhook(api_client, event, [_polar_subscription(cancel_at_period_end=True)])

    assert Membership.objects.get(user=user).cancel_at_period_end is True


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_revoked_ends_membership(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")

    _post_webhook(api_client, _membership_event("subscription.revoked", user.id, status="canceled"), [])

    assert not Membership.objects.filter(user=user).exists()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_late_event_for_old_subscription_keeps_newer_membership(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_new")
    event = _membership_event("subscription.revoked", user.id, status="canceled", subscription_id="sub_old")

    _post_webhook(api_client, event, [_polar_subscription(subscription_id="sub_new", product_id="prod_canopy")])

    assert Membership.objects.get(user=user).polar_subscription_id == "sub_new"


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_retried_live_event_does_not_revive_ended_membership(api_client, user):
    event = _membership_event("subscription.updated", user.id, status="active")

    _post_webhook(api_client, event, [])

    assert not Membership.objects.filter(user=user).exists()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_live_event_for_old_subscription_does_not_replace_newer(api_client, user):
    Membership.objects.create(user=user, tier=MembershipTier.CANOPY, polar_subscription_id="sub_new")
    event = _membership_event("subscription.updated", user.id, product_id="prod_sprout", subscription_id="sub_old")

    _post_webhook(api_client, event, [_polar_subscription(subscription_id="sub_new", product_id="prod_canopy")])

    member = Membership.objects.get(user=user)
    assert member.polar_subscription_id == "sub_new"
    assert member.tier == MembershipTier.CANOPY


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_duplicate_subscriptions_keep_newest_and_cancel_the_rest(api_client, user):
    older = _polar_subscription(
        subscription_id="sub_sprout", product_id="prod_sprout", created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)
    )
    newer = _polar_subscription(
        subscription_id="sub_canopy", product_id="prod_canopy", created_at=datetime(2026, 10, 2, tzinfo=timezone.utc)
    )
    event = _membership_event("subscription.active", user.id, product_id="prod_sprout", subscription_id="sub_sprout")

    _, mock_polar = _post_webhook(api_client, event, [older, newer])

    member = Membership.objects.get(user=user)
    assert member.polar_subscription_id == "sub_canopy"
    assert member.tier == MembershipTier.CANOPY
    update_params = mock_polar.subscriptions.update.call_args[1]
    assert update_params["id"] == "sub_sprout"
    assert update_params["subscription_update"].cancel_at_period_end is True


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_duplicate_already_cancelling_is_left_alone(api_client, user):
    older = _polar_subscription(subscription_id="sub_sprout", cancel_at_period_end=True)
    newer = _polar_subscription(subscription_id="sub_canopy", created_at=datetime(2026, 10, 2, tzinfo=timezone.utc))

    _, mock_polar = _post_webhook(api_client, _membership_event("subscription.updated", user.id), [older, newer])

    mock_polar.subscriptions.update.assert_not_called()


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_past_due_keeps_membership_and_join_date(api_client, user):
    member = Membership.objects.create(user=user, tier=MembershipTier.SAPLING, polar_subscription_id="sub_member")
    event = _membership_event("subscription.past_due", user.id, status="past_due")

    _, mock_polar = _post_webhook(api_client, event, [_polar_subscription()])

    assert Membership.objects.get(user=user).started == member.started
    assert "past_due" in mock_polar.subscriptions.list.call_args[1]["status"]


@pytest.mark.django_db
@PRODUCT_SETTINGS
def test_webhook_retired_product_is_not_treated_as_usage_billing(api_client, user):
    UserBilling.objects.create(user=user, polar_subscription_id="sub_usage", max_monthly_spend_cents=1_000)
    Membership.objects.create(user=user, tier=MembershipTier.SPROUT, polar_subscription_id="sub_member")
    event = _membership_event("subscription.revoked", user.id, product_id="prod_retired", status="canceled")

    _post_webhook(api_client, event, [])

    billing = UserBilling.objects.get(user=user)
    assert billing.polar_subscription_id == "sub_usage"
    assert billing.max_monthly_spend_cents == 1_000
    assert not Membership.objects.filter(user=user).exists()
