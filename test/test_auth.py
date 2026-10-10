from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from ninja_jwt.tokens import RefreshToken

User = get_user_model()

TOKEN_URL = "/api/v1/platform/token/pair"
REFRESH_URL = "/api/v1/platform/token/refresh"


@pytest.fixture
def verified_user(db):
    user = User.objects.create_user(
        username="swift_falcon_379",
        email="user@example.com",
        password="correctpassword",
    )
    EmailAddress.objects.create(user=user, email="user@example.com", verified=True, primary=True)
    return user


@pytest.fixture
def unverified_user(db):
    user = User.objects.create_user(
        username="quiet_river_123",
        email="unverified@example.com",
        password="correctpassword",
    )
    EmailAddress.objects.create(user=user, email="unverified@example.com", verified=False, primary=True)
    return user


@pytest.mark.django_db
def test_login_with_username(client, verified_user):
    response = client.post(
        TOKEN_URL,
        {"username": "swift_falcon_379", "password": "correctpassword"},
        content_type="application/json",
    )
    assert response.status_code == 200
    data = response.json()
    assert "access" in data
    assert "refresh" in data


@pytest.mark.django_db
def test_login_with_email(client, verified_user):
    response = client.post(
        TOKEN_URL,
        {"username": "user@example.com", "password": "correctpassword"},
        content_type="application/json",
    )
    assert response.status_code == 200
    data = response.json()
    assert "access" in data
    assert "refresh" in data


@pytest.mark.django_db
def test_login_wrong_password(client, verified_user):
    response = client.post(
        TOKEN_URL,
        {"username": "swift_falcon_379", "password": "wrongpassword"},
        content_type="application/json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_login_nonexistent_user(client, db):
    response = client.post(
        TOKEN_URL,
        {"username": "nobody", "password": "somepassword"},
        content_type="application/json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_login_unverified_email_rejected(client, unverified_user):
    response = client.post(
        TOKEN_URL,
        {"username": "quiet_river_123", "password": "correctpassword"},
        content_type="application/json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_login_email_and_username_return_same_user(client, verified_user):
    by_username = client.post(
        TOKEN_URL,
        {"username": "swift_falcon_379", "password": "correctpassword"},
        content_type="application/json",
    ).json()
    by_email = client.post(
        TOKEN_URL,
        {"username": "user@example.com", "password": "correctpassword"},
        content_type="application/json",
    ).json()
    # Both tokens are for the same user — decode the subject claim to verify
    import base64
    import json as _json

    def subject(token):
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return _json.loads(base64.urlsafe_b64decode(payload))["user_id"]

    assert subject(by_username["access"]) == subject(by_email["access"])


def _obtain_pair(client):
    return client.post(
        TOKEN_URL,
        {"username": "swift_falcon_379", "password": "correctpassword"},
        content_type="application/json",
    ).json()


def _refresh(client, refresh):
    return client.post(REFRESH_URL, {"refresh": refresh}, content_type="application/json")


@pytest.mark.django_db
def test_refresh_rotates_refresh_token(client, verified_user):
    # Rotation restarts the refresh token lifetime, so active users stay logged in
    pair = _obtain_pair(client)
    response = _refresh(client, pair["refresh"])
    assert response.status_code == 200
    data = response.json()
    assert data["access"]
    assert data["refresh"] != pair["refresh"]

    rotated = RefreshToken(data["refresh"])
    assert rotated["exp"] - rotated["iat"] == timedelta(days=365).total_seconds()
    assert _refresh(client, data["refresh"]).status_code == 200


@pytest.mark.django_db
def test_rotated_out_refresh_token_is_rejected(client, verified_user):
    pair = _obtain_pair(client)
    assert _refresh(client, pair["refresh"]).status_code == 200

    response = _refresh(client, pair["refresh"])
    assert response.status_code == 401


@pytest.mark.django_db
def test_refresh_rejected_for_deactivated_user(client, verified_user):
    pair = _obtain_pair(client)
    verified_user.is_active = False
    verified_user.save()

    response = _refresh(client, pair["refresh"])
    assert response.status_code == 401


@pytest.mark.django_db
def test_refresh_rejected_for_deleted_user(client, verified_user):
    pair = _obtain_pair(client)
    verified_user.delete()

    response = _refresh(client, pair["refresh"])
    assert response.status_code == 401
