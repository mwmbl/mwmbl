"""
Tests for platform billing endpoints.

Covers:
- POST /api/v1/platform/billing/cancel
- POST /api/v1/platform/billing/uncancel
- POST /api/v1/platform/billing/spend-limit
- POST /api/v1/platform/billing/checkout
- POST /api/v1/platform/billing/webhook
"""

from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import AgreementType, UserAgreement, UserBilling

User = get_user_model()


@pytest.fixture
def verified_user_with_billing(db):
    """Create a verified user with billing record and subscription."""
    user = User.objects.create_user(
        username="billinguser",
        email="billing@example.com",
        password="testpass123",
    )
    EmailAddress.objects.create(
        user=user,
        email="billing@example.com",
        verified=True,
        primary=True,
    )
    for agreement_type in (AgreementType.TERMS_OF_SERVICE_API, AgreementType.TERMS_OF_SERVICE_GUI):
        version_id = "v2026-04-A"
        UserAgreement.objects.create(
            user=user,
            agreement_type=agreement_type,
            version_id=version_id,
        )
    UserBilling.objects.create(
        user=user,
        polar_customer_id="cust_test123",
        polar_subscription_id="sub_test123",
        current_period_end=None,
        cancel_at_period_end=False,
        max_monthly_spend_cents=1_000,
    )
    return user


@pytest.fixture
def access_token(verified_user_with_billing):
    refresh = RefreshToken.for_user(verified_user_with_billing)
    return str(refresh.access_token)


@pytest.fixture
def api_client(db):
    """Return a Django test client pointed at the v1 API."""
    return Client()


def auth_headers(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Cancel subscription tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_cancel_subscription_success(api_client, access_token, verified_user_with_billing):
    """Test successful subscription cancellation at period end."""
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    assert billing.cancel_at_period_end is False

    with patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        mock_result = mock_polar_instance.subscriptions.update.return_value
        mock_result.current_period_end = None

        response = api_client.post(
            "/api/v1/platform/billing/cancel",
            content_type="application/json",
            **auth_headers(access_token),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "canceling"

    billing.refresh_from_db()
    assert billing.cancel_at_period_end is True


@pytest.mark.django_db
def test_cancel_subscription_already_canceled(api_client, access_token, verified_user_with_billing):
    """Test that canceling an already canceled subscription returns 409."""
    from unittest.mock import Mock
    from polar_sdk.models import AlreadyCanceledSubscription

    billing = UserBilling.objects.get(user=verified_user_with_billing)
    billing.cancel_at_period_end = True
    billing.save()

    with patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        
        # Create a mock response
        mock_response = Mock()
        mock_response.status_code = 409
        mock_response.text = "Subscription is already canceled"
        
        # Create AlreadyCanceledSubscription with proper arguments
        mock_data = Mock()
        mock_data.detail = "Subscription is already canceled"
        mock_polar_instance.subscriptions.update.side_effect = AlreadyCanceledSubscription(
            data=mock_data, raw_response=mock_response
        )

        response = api_client.post(
            "/api/v1/platform/billing/cancel",
            content_type="application/json",
            **auth_headers(access_token),
        )

    assert response.status_code == 409
    data = response.json()
    assert data["status"] == "error"
    assert "already canceled" in data["message"].lower()


@pytest.mark.django_db
def test_cancel_subscription_unauthenticated(api_client):
    """Test that canceling without authentication returns 401."""
    response = api_client.post("/api/v1/platform/billing/cancel", content_type="application/json")
    assert response.status_code == 401


@pytest.mark.django_db
def test_cancel_subscription_unverified_email(api_client, access_token, verified_user_with_billing):
    """Test that canceling without a subscription returns 404."""
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    billing.polar_subscription_id = ""
    billing.save()

    response = api_client.post(
        "/api/v1/platform/billing/cancel",
        content_type="application/json",
        **auth_headers(access_token),
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Uncancel subscription tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_uncancel_subscription_success(api_client, access_token, verified_user_with_billing):
    """Test successful uncancel of a pending cancellation."""
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    billing.cancel_at_period_end = True
    billing.save()

    with patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        mock_result = mock_polar_instance.subscriptions.update.return_value
        mock_result.current_period_end = None

        response = api_client.post(
            "/api/v1/platform/billing/uncancel",
            content_type="application/json",
            **auth_headers(access_token),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"

    billing.refresh_from_db()
    assert billing.cancel_at_period_end is False


@pytest.mark.django_db
def test_uncancel_subscription_not_scheduled_to_cancel(api_client, access_token, verified_user_with_billing):
    """Test that uncanceling when not scheduled returns 409."""
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    billing.cancel_at_period_end = False
    billing.save()

    response = api_client.post(
        "/api/v1/platform/billing/uncancel",
        content_type="application/json",
        **auth_headers(access_token),
    )

    assert response.status_code == 409
    data = response.json()
    assert data["status"] == "error"
    assert "not scheduled" in data["message"].lower()


@pytest.mark.django_db
def test_uncancel_subscription_unauthenticated(api_client):
    """Test that uncanceling without authentication returns 401."""
    response = api_client.post("/api/v1/platform/billing/uncancel", content_type="application/json")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Spend limit tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_spend_limit_requires_subscription_first(api_client, access_token, db):
    """Raising the spend limit above $0 without an active subscription returns 409."""
    user = User.objects.create_user(
        username="nobilling", email="nobilling@example.com", password="testpass123",
    )
    EmailAddress.objects.create(user=user, email="nobilling@example.com", verified=True, primary=True)
    token = str(RefreshToken.for_user(user).access_token)

    response = api_client.post(
        "/api/v1/platform/billing/spend-limit",
        content_type="application/json",
        data={"max_monthly_spend_cents": 1_000},
        **auth_headers(token),
    )

    assert response.status_code == 409


@pytest.mark.django_db
def test_update_spend_limit_success(api_client, access_token, verified_user_with_billing):
    """Raising the spend limit with an active subscription succeeds."""
    response = api_client.post(
        "/api/v1/platform/billing/spend-limit",
        content_type="application/json",
        data={"max_monthly_spend_cents": 2_500},
        **auth_headers(access_token),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["max_monthly_spend_cents"] == 2_500
    assert data["status"] == "active"

    billing = UserBilling.objects.get(user=verified_user_with_billing)
    assert billing.max_monthly_spend_cents == 2_500


@pytest.mark.django_db
def test_update_spend_limit_reset_to_zero_always_allowed(api_client, access_token, verified_user_with_billing):
    """Lowering the spend limit to 0 doesn't require any subscription check."""
    response = api_client.post(
        "/api/v1/platform/billing/spend-limit",
        content_type="application/json",
        data={"max_monthly_spend_cents": 0},
        **auth_headers(access_token),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["max_monthly_spend_cents"] == 0
    assert data["status"] == "free"


@pytest.mark.django_db
def test_update_spend_limit_unauthenticated(api_client):
    response = api_client.post(
        "/api/v1/platform/billing/spend-limit",
        content_type="application/json",
        data={"max_monthly_spend_cents": 1_000},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Checkout tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_checkout_uses_single_usage_product(api_client, access_token, verified_user_with_billing):
    """Checkout always targets the single usage product and always sets external_customer_id."""
    from django.conf import settings

    with patch.object(settings, "POLAR_PRODUCT_ID_USAGE", "prod_usage123"), \
         patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        mock_polar_instance.checkouts.create.return_value.url = "https://polar.example/checkout/abc"

        response = api_client.post(
            "/api/v1/platform/billing/checkout",
            content_type="application/json",
            data={},
            **auth_headers(access_token),
        )

    assert response.status_code == 200
    assert response.json()["checkout_url"] == "https://polar.example/checkout/abc"

    call_kwargs = mock_polar_instance.checkouts.create.call_args[1]
    checkout_params = call_kwargs["request"]
    assert checkout_params["products"] == ["prod_usage123"]
    assert checkout_params["external_customer_id"] == str(verified_user_with_billing.id)


@pytest.mark.django_db
def test_checkout_not_configured_returns_503(api_client, access_token, verified_user_with_billing):
    from django.conf import settings

    with patch.object(settings, "POLAR_PRODUCT_ID_USAGE", ""):
        response = api_client.post(
            "/api/v1/platform/billing/checkout",
            content_type="application/json",
            data={},
            **auth_headers(access_token),
        )

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Webhook tests
# ---------------------------------------------------------------------------


def _mock_webhook_event(event_type, user_id, **data_overrides):
    from unittest.mock import Mock
    event = Mock()
    event.TYPE = event_type
    event.data = Mock()
    event.data.metadata = {"user_id": str(user_id)}
    event.data.customer_id = data_overrides.get("customer_id", "cust_new")
    event.data.id = data_overrides.get("subscription_id", "sub_new")
    event.data.current_period_end = data_overrides.get("current_period_end", None)
    event.data.cancel_at_period_end = data_overrides.get("cancel_at_period_end", False)
    return event


@pytest.mark.django_db
def test_webhook_subscription_active_sets_billing_fields_not_spend_limit(api_client, verified_user_with_billing):
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    billing.max_monthly_spend_cents = 2_500
    billing.save()

    event = _mock_webhook_event("subscription.active", verified_user_with_billing.id, customer_id="cust_abc", subscription_id="sub_abc")
    with patch("mwmbl.platform.api.validate_event", return_value=event):
        response = api_client.post(
            "/api/v1/platform/billing/webhook",
            content_type="application/json",
            data={},
        )

    assert response.status_code == 200
    billing.refresh_from_db()
    assert billing.polar_customer_id == "cust_abc"
    assert billing.polar_subscription_id == "sub_abc"
    # Spend limit is untouched by subscription state changes.
    assert billing.max_monthly_spend_cents == 2_500


@pytest.mark.django_db
def test_webhook_subscription_canceled_immediate_resets_spend_limit_to_zero(api_client, verified_user_with_billing):
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    assert billing.max_monthly_spend_cents == 1_000

    event = _mock_webhook_event("subscription.canceled", verified_user_with_billing.id, cancel_at_period_end=False)
    with patch("mwmbl.platform.api.validate_event", return_value=event):
        response = api_client.post(
            "/api/v1/platform/billing/webhook",
            content_type="application/json",
            data={},
        )

    assert response.status_code == 200
    billing.refresh_from_db()
    assert billing.max_monthly_spend_cents == 0
    assert billing.cancel_at_period_end is False


@pytest.mark.django_db
def test_webhook_subscription_canceled_scheduled_does_not_reset_spend_limit(api_client, verified_user_with_billing):
    event = _mock_webhook_event("subscription.canceled", verified_user_with_billing.id, cancel_at_period_end=True)
    with patch("mwmbl.platform.api.validate_event", return_value=event):
        response = api_client.post(
            "/api/v1/platform/billing/webhook",
            content_type="application/json",
            data={},
        )

    assert response.status_code == 200
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    assert billing.max_monthly_spend_cents == 1_000
    assert billing.cancel_at_period_end is True


@pytest.mark.django_db
def test_webhook_subscription_revoked_resets_spend_limit_to_zero(api_client, verified_user_with_billing):
    event = _mock_webhook_event("subscription.revoked", verified_user_with_billing.id)
    with patch("mwmbl.platform.api.validate_event", return_value=event):
        response = api_client.post(
            "/api/v1/platform/billing/webhook",
            content_type="application/json",
            data={},
        )

    assert response.status_code == 200
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    assert billing.max_monthly_spend_cents == 0
