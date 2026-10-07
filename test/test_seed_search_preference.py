import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from ninja_jwt.tokens import RefreshToken

User = get_user_model()

PREFERENCE_URL = "/api/v1/platform/combined-search/preference"
REGISTER_URL = "/api/v1/platform/register"


@pytest.fixture
def user(db):
    return User.objects.create_user(username="seeder", email="seeder@example.com", password="x")


@pytest.fixture
def auth(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


@pytest.fixture
def client(db):
    return Client()


def _set(client, auth, enabled):
    return client.put(PREFERENCE_URL, data=json.dumps({"enabled": enabled}), content_type="application/json", **auth)


@pytest.mark.django_db
def test_a_newly_registered_user_has_seed_search_on(client):
    body = {"email": "new@example.com", "password": "correctpassword", "username": "newbie"}
    client.post(REGISTER_URL, data=json.dumps(body), content_type="application/json")

    assert User.objects.get(username="newbie").seed_search_enabled


@pytest.mark.django_db
def test_the_preference_endpoint_reports_the_users_setting(client, user, auth):
    response = client.get(PREFERENCE_URL, **auth)

    assert response.status_code == 200
    assert response.json() == {"enabled": True}


@pytest.mark.django_db
def test_switching_seed_search_off_is_saved(client, user, auth):
    response = _set(client, auth, False)

    user.refresh_from_db()
    assert response.json() == {"enabled": False}
    assert not user.seed_search_enabled
    assert client.get(PREFERENCE_URL, **auth).json() == {"enabled": False}


@pytest.mark.django_db
def test_switching_seed_search_back_on_is_saved(client, user, auth):
    _set(client, auth, False)
    _set(client, auth, True)

    user.refresh_from_db()
    assert user.seed_search_enabled


@pytest.mark.django_db
def test_the_preference_endpoints_require_a_jwt(client):
    assert client.get(PREFERENCE_URL).status_code == 401
    assert client.put(PREFERENCE_URL, data="{}", content_type="application/json").status_code == 401
