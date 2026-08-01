"""
Tests for the report_usage_to_polar background task.

Covers incremental-delta ingestion, skipping users with no linked Polar
customer, and no-op behavior below the free allowance.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model

from mwmbl import pricing
from mwmbl.background import report_usage_to_polar
from mwmbl.models import UsageBucket, UserBilling

User = get_user_model()


@pytest.fixture
def billed_user(db):
    user = User.objects.create_user(
        username="billeduser", email="billed@example.com", password="testpass123",
    )
    EmailAddress.objects.create(user=user, email="billed@example.com", verified=True, primary=True)
    UserBilling.objects.create(
        user=user,
        polar_customer_id="cust_test123",
        polar_subscription_id="sub_test123",
        max_monthly_spend_cents=10_000,
    )
    return user


@pytest.mark.django_db
def test_report_usage_to_polar_ingests_delta_only(billed_user):
    now = datetime.now(timezone.utc)
    bucket = UsageBucket.objects.create(
        user=billed_user, year=now.year, month=now.month,
        count=pricing.FREE_KEYED_MONTHLY_LIMIT + 500, reported_overage=0,
    )

    with patch("polar_sdk.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        report_usage_to_polar.now()

    mock_polar_instance.events.ingest.assert_called_once()
    call_kwargs = mock_polar_instance.events.ingest.call_args[1]
    events = call_kwargs["request"]["events"]
    assert len(events) == 1
    assert events[0]["external_customer_id"] == str(billed_user.id)
    assert events[0]["metadata"]["quantity"] == 500

    bucket.refresh_from_db()
    assert bucket.reported_overage == 500


@pytest.mark.django_db
def test_report_usage_to_polar_skips_users_without_polar_customer(db):
    user = User.objects.create_user(
        username="nocustomer", email="nocustomer@example.com", password="testpass123",
    )
    EmailAddress.objects.create(user=user, email="nocustomer@example.com", verified=True, primary=True)
    now = datetime.now(timezone.utc)
    UsageBucket.objects.create(
        user=user, year=now.year, month=now.month,
        count=pricing.FREE_KEYED_MONTHLY_LIMIT + 500, reported_overage=0,
    )

    with patch("polar_sdk.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        report_usage_to_polar.now()

    mock_polar_instance.events.ingest.assert_not_called()


@pytest.mark.django_db
def test_report_usage_to_polar_no_op_below_free_allowance(billed_user):
    now = datetime.now(timezone.utc)
    bucket = UsageBucket.objects.create(
        user=billed_user, year=now.year, month=now.month,
        count=pricing.FREE_KEYED_MONTHLY_LIMIT - 1, reported_overage=0,
    )

    with patch("polar_sdk.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        report_usage_to_polar.now()

    mock_polar_instance.events.ingest.assert_not_called()
    bucket.refresh_from_db()
    assert bucket.reported_overage == 0


@pytest.mark.django_db
def test_report_usage_to_polar_incremental_second_run(billed_user):
    now = datetime.now(timezone.utc)
    bucket = UsageBucket.objects.create(
        user=billed_user, year=now.year, month=now.month,
        count=pricing.FREE_KEYED_MONTHLY_LIMIT + 500, reported_overage=500,
    )

    # More usage has accumulated since the last report.
    bucket.count = pricing.FREE_KEYED_MONTHLY_LIMIT + 800
    bucket.save()

    with patch("polar_sdk.Polar") as MockPolar:
        mock_polar_instance = MockPolar.return_value.__enter__.return_value
        report_usage_to_polar.now()

    call_kwargs = mock_polar_instance.events.ingest.call_args[1]
    events = call_kwargs["request"]["events"]
    assert len(events) == 1
    assert events[0]["metadata"]["quantity"] == 300  # only the new delta

    bucket.refresh_from_db()
    assert bucket.reported_overage == 800
