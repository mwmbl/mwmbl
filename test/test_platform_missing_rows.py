"""
Platform endpoints that act on a row by id or username must answer 404 when the row is missing,
not 500 from an unhandled DoesNotExist.
"""

import json

import pytest
from allauth.account.models import EmailAddress
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import DomainSubmission, MwmblUser


@pytest.fixture
def verified_user(db):
    user = MwmblUser.objects.create_user(username="caller", email="caller@example.com", password="password")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user


@pytest.fixture
def access_token(verified_user):
    return str(RefreshToken.for_user(verified_user).access_token)


@pytest.mark.django_db
def test_deleting_an_unknown_user_is_a_404(client, access_token):
    response = client.delete(
        "/api/v1/platform/users/nobody",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_deleting_an_unknown_submission_is_a_404(client, access_token):
    response = client.delete(
        "/api/v1/platform/domain-submissions/ids/999999",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_updating_an_unknown_submission_is_a_404(client, access_token):
    response = client.post(
        "/api/v1/platform/domain-submissions/ids/999999",
        data=json.dumps({"status": "APPROVED", "rejection_reason": "", "rejection_detail": ""}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 404
    assert DomainSubmission.objects.count() == 0
