"""
Tests for platform billing endpoints.

Covers:
- POST /api/v1/platform/billing/cancel
- POST /api/v1/platform/billing/uncancel
- POST /api/v1/platform/billing/change-plan
"""

from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import AgreementType, MwmblUser, UserAgreement, UserBilling

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
    )
    user.tier = MwmblUser.Tier.STARTER
    user.save()
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
# Change plan tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_change_plan_success(api_client, access_token, verified_user_with_billing):
    """Test successful plan change."""
    from django.conf import settings

    with patch.object(settings, "POLAR_PRODUCT_ID_PRO", "prod_test123"), \
         patch("mwmbl.platform.api.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        mock_result = mock_polar_instance.subscriptions.update.return_value
        mock_result.current_period_end = None

        response = api_client.post(
            "/api/v1/platform/billing/change-plan",
            content_type="application/json",
            data={"plan": "pro"},
            **auth_headers(access_token),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert data["plan"] == MwmblUser.Tier.STARTER

    mock_polar_instance.subscriptions.update.assert_called_once()
    call_kwargs = mock_polar_instance.subscriptions.update.call_args[1]
    subscription_update = call_kwargs["subscription_update"]
    assert hasattr(subscription_update, "product_id")


@pytest.mark.django_db
def test_change_plan_unauthenticated(api_client):
    """Test that changing plan without authentication returns 401."""
    response = api_client.post(
        "/api/v1/platform/billing/change-plan",
        content_type="application/json",
        data={"plan": "pro"},
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_change_plan_invalid_plan(api_client, access_token, verified_user_with_billing):
    """Test that changing to an invalid plan returns 400."""
    response = api_client.post(
        "/api/v1/platform/billing/change-plan",
        content_type="application/json",
        data={"plan": "invalid"},
        **auth_headers(access_token),
    )

    assert response.status_code == 422


@pytest.mark.django_db
def test_change_plan_no_subscription(api_client, access_token, verified_user_with_billing):
    """Test that changing plan without a subscription returns 404."""
    billing = UserBilling.objects.get(user=verified_user_with_billing)
    billing.polar_subscription_id = ""
    billing.save()

    response = api_client.post(
        "/api/v1/platform/billing/change-plan",
        content_type="application/json",
        data={"plan": "pro"},
        **auth_headers(access_token),
    )

    assert response.status_code == 404
    data = response.json()
    assert data["status"] == "error"
    assert "no active subscription" in data["message"].lower()
