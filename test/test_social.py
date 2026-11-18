"""The Mwmbl Social sign-up endpoints (mwmbl.platform.social_api), with its admin API faked at the
HTTP boundary by pytest-httpx. The client, addresses and limits are in test_social_client.py."""

import json

import httpx
import pytest
from django.core.cache import cache
from ninja_jwt.tokens import RefreshToken

from mwmbl import social
from mwmbl.models import MwmblUser, SocialRegistration

REGISTRATIONS = "/api/v2/social/registrations"
API = "https://social.test/api/v1/admin/registrations"
CREATED = {
    "id": 4,
    "base_url": "https://mastodon.example/api/fasp",
    "status": "pending",
    "capabilities": [],
    "fingerprint": "SHA256:abc123",
    "created_at": 1763337600,
    "approval_url": "https://mastodon.example/admin/fasp/providers/7/registration/new",
}


@pytest.fixture(autouse=True)
def empty_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return make_user("fedi_admin")


def make_user(username: str) -> MwmblUser:
    return MwmblUser.objects.create_user(username=username, email=f"{username}@example.com", password="password")


def auth(user) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def connect(client, user, server_url: str):
    return client.post(REGISTRATIONS, {"server_url": server_url}, content_type="application/json", **auth(user))


def stored_registration(user, **fields) -> SocialRegistration:
    values = {
        "server_url": "https://mastodon.example",
        "registration_id": CREATED["id"],
        "base_url": CREATED["base_url"],
        "fingerprint": CREATED["fingerprint"],
        "approval_url": CREATED["approval_url"],
    }
    return SocialRegistration.objects.create(user=user, **(values | fields))


@pytest.mark.parametrize("method", ["get", "post"])
def test_registrations_need_a_login(client, db, method):
    assert getattr(client, method)(REGISTRATIONS).status_code == 401


def test_connecting_registers_the_server(client, user, httpx_mock):
    httpx_mock.add_response(method="POST", url=API, status_code=201, json=CREATED)

    response = connect(client, user, "Mastodon.example")

    registration = SocialRegistration.objects.get()
    assert response.status_code == 201
    assert response.json() | {"created_at": None} == {
        "id": registration.id,
        "server_url": "https://mastodon.example",
        "base_url": CREATED["base_url"],
        "status": "pending",
        "capabilities": [],
        "fingerprint": CREATED["fingerprint"],
        "approval_url": CREATED["approval_url"],
        "created_at": None,
        "disconnected_at": None,
    }
    assert (registration.user, registration.registration_id) == (user, 4)
    assert json.loads(httpx_mock.get_request().content) == {"server_url": "https://mastodon.example"}


def test_an_approval_link_that_is_not_https_is_not_kept(client, user, httpx_mock):
    httpx_mock.add_response(method="POST", url=API, status_code=201, json=CREATED | {"approval_url": "javascript:x"})

    response = connect(client, user, "mastodon.example")

    assert response.json()["approval_url"] is None
    assert SocialRegistration.objects.get().approval_url == ""


@pytest.mark.parametrize(
    "status, code", [(400, "invalid_address"), (409, "already_registered"), (502, "server_unreachable")]
)
def test_refusals_are_explained(client, user, httpx_mock, status, code):
    httpx_mock.add_response(method="POST", url=API, status_code=status, json={"error": "no", "id": 1})

    response = connect(client, user, "mastodon.example")

    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["message"]
    assert not SocialRegistration.objects.exists()


def test_an_unanswered_registration_says_to_check_the_admin_panel(client, user, httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"))

    response = connect(client, user, "mastodon.example")

    assert response.status_code == 503
    assert response.json()["code"] == "social_unavailable"
    assert "delete it there before trying again" in response.json()["message"]


def test_a_bad_address_is_refused_without_calling_the_api_or_using_an_attempt(client, user, settings):
    settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY = 1

    response = connect(client, user, "http://mastodon.example")

    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_address",
        "message": "Enter your server's https address.",
        "retry_after": None,
    }
    assert social.user_attempt_wait(user.id) is None


def test_each_user_has_a_daily_limit(client, user, httpx_mock, settings):
    settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY = 2
    httpx_mock.add_response(method="POST", url=API, status_code=502, json={"error": "no"}, is_reusable=True)

    responses = [connect(client, user, f"server{n}.example") for n in range(3)]

    assert [response.status_code for response in responses] == [502, 502, 429]
    refusal = responses[-1]
    assert refusal.json()["code"] == "user_limit"
    assert 0 < refusal.json()["retry_after"] <= 24 * 60 * 60
    assert refusal["Retry-After"] == str(refusal.json()["retry_after"])
    assert len(httpx_mock.get_requests()) == 2


def test_each_server_gets_one_attempt_an_hour_whoever_makes_it(client, user, httpx_mock):
    httpx_mock.add_response(method="POST", url=API, status_code=502, json={"error": "no"})
    connect(client, user, "mastodon.example")

    response = connect(client, make_user("someone_else"), "https://mastodon.example/")

    assert response.status_code == 429
    assert response.json()["code"] == "host_limit"
    assert 0 < response.json()["retry_after"] <= 60 * 60


@pytest.mark.parametrize(
    "live, status, capabilities",
    [
        ([], "replaced", []),
        ([CREATED | {"status": "active", "capabilities": ["account_search@0.1"]}], "active", ["account_search@0.1"]),
    ],
)
def test_the_list_shows_live_status(client, user, httpx_mock, live, status, capabilities):
    stored_registration(user)
    httpx_mock.add_response(method="GET", url=API, json=live)

    [row] = client.get(REGISTRATIONS, **auth(user)).json()

    assert (row["status"], row["capabilities"]) == (status, capabilities)


def test_the_list_says_when_the_status_is_unavailable(client, user, httpx_mock):
    stored_registration(user)
    stored_registration(user, registration_id=5, disconnected_at="2026-11-18T00:00:00Z")
    httpx_mock.add_response(method="GET", url=API, status_code=500)

    response = client.get(REGISTRATIONS, **auth(user))

    assert response.status_code == 200
    assert {row["status"] for row in response.json()} == {"unavailable", "disconnected"}


def test_users_see_and_disconnect_only_their_own_registrations(client, user, db):
    registration = stored_registration(user)
    someone_else = auth(make_user("someone_else"))

    assert client.get(REGISTRATIONS, **someone_else).json() == []
    assert client.get(f"{REGISTRATIONS}/{registration.id}", **someone_else).status_code == 404
    assert client.delete(f"{REGISTRATIONS}/{registration.id}", **someone_else).status_code == 404
    registration.refresh_from_db()
    assert registration.disconnected_at is None


def test_one_registration_has_its_live_status(client, user, httpx_mock):
    registration = stored_registration(user)
    httpx_mock.add_response(method="GET", url=API, json=[CREATED | {"status": "active"}])

    response = client.get(f"{REGISTRATIONS}/{registration.id}", **auth(user))

    assert (response.json()["id"], response.json()["status"]) == (registration.id, "active")


@pytest.mark.parametrize("api_status", [200, 404])
def test_disconnecting_revokes_the_registration_once(client, user, httpx_mock, api_status):
    registration = stored_registration(user)
    httpx_mock.add_response(method="DELETE", url=f"{API}/4", status_code=api_status, json={})

    responses = [client.delete(f"{REGISTRATIONS}/{registration.id}", **auth(user)) for _ in range(2)]

    assert [response.status_code for response in responses] == [204, 204]
    assert len(httpx_mock.get_requests()) == 1
    [row] = client.get(REGISTRATIONS, **auth(user)).json()
    assert row["status"] == "disconnected"
