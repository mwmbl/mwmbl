"""Connecting fediverse servers to Mwmbl Social (mwmbl.social, mwmbl.social_views), with its admin
API faked at the HTTP boundary by pytest-httpx."""

import json

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from mwmbl import social
from mwmbl.models import SocialRegistration

User = get_user_model()

CONNECT = "/social/connect/"
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


def make_user(username: str):
    return User.objects.create_user(username=username, email=f"{username}@example.com", password="password")


@pytest.fixture
def admin_client(client, db):
    client.force_login(make_user("fedi_admin"))
    return client


def connect(client, server_url: str):
    return client.post(CONNECT, {"server_url": server_url})


def stored_registration(user_username: str = "fedi_admin", **fields) -> SocialRegistration:
    values = {
        "server_url": "https://mastodon.example",
        "registration_id": CREATED["id"],
        "base_url": CREATED["base_url"],
        "fingerprint": CREATED["fingerprint"],
        "approval_url": CREATED["approval_url"],
    }
    return SocialRegistration.objects.create(user=User.objects.get(username=user_username), **(values | fields))


@pytest.mark.parametrize(
    "entered, expected",
    [
        ("mastodon.example", ("https://mastodon.example", "mastodon.example")),
        ("https://Mastodon.Example/about", ("https://mastodon.example", "mastodon.example")),
        ("https://m.example:8443", ("https://m.example:8443", "m.example:8443")),
    ],
)
def test_a_server_address_is_normalised_to_https_and_its_host(entered, expected):
    assert social.server_url_and_host(entered) == expected


@pytest.mark.parametrize("entered", ["http://mastodon.example", "https://", "https://me:pw@mastodon.example"])
def test_unusable_server_addresses_are_refused(entered):
    with pytest.raises(ValueError):
        social.server_url_and_host(entered)


def test_logged_out_users_are_asked_to_log_in_and_come_back(client, db):
    response = client.get(CONNECT)

    assert response.status_code == 200
    assert b"/accounts/login/?next=/social/connect/" in response.content
    assert b'<form method="post"' not in response.content


def test_a_logged_out_post_redirects_to_login(client, db):
    response = connect(client, "mastodon.example")

    assert response.status_code == 302
    assert response["Location"] == "/accounts/login/?next=/social/connect/"


def test_connecting_registers_the_server_and_shows_how_to_approve_it(admin_client, httpx_mock):
    httpx_mock.add_response(method="POST", url=API, status_code=201, json=CREATED)
    httpx_mock.add_response(method="GET", url=API, json=[CREATED | {"approval_url": None}])

    response = connect(admin_client, "Mastodon.example")

    registration = SocialRegistration.objects.get()
    assert response.status_code == 302
    assert response["Location"] == f"{CONNECT}#registration-{registration.pk}"
    assert (registration.server_url, registration.registration_id) == ("https://mastodon.example", 4)
    assert registration.approval_url == CREATED["approval_url"]
    sent = httpx_mock.get_request(method="POST")
    assert sent.headers["Authorization"] == "Bearer test-admin-token"
    assert json.loads(sent.content) == {"server_url": "https://mastodon.example"}

    page = admin_client.get(CONNECT).content.decode()
    assert "Waiting for the server admin&#x27;s approval" in page
    assert CREATED["fingerprint"] in page
    assert f'href="{CREATED["approval_url"]}"' in page


def test_an_approval_link_that_is_not_https_is_not_kept(admin_client, httpx_mock):
    httpx_mock.add_response(method="POST", url=API, status_code=201, json=CREATED | {"approval_url": "javascript:x"})

    connect(admin_client, "mastodon.example")

    assert SocialRegistration.objects.get().approval_url == ""


@pytest.mark.parametrize(
    "status, message",
    [(400, "can&#x27;t use that address"), (409, "already connected"), (502, "EXPERIMENTAL_FEATURES=fasp")],
)
def test_refusals_are_explained(admin_client, httpx_mock, status, message):
    httpx_mock.add_response(method="POST", url=API, status_code=status, json={"error": "no", "id": 1})

    response = connect(admin_client, "mastodon.example")

    assert response.status_code == status
    assert message in response.content.decode()
    assert not SocialRegistration.objects.exists()


def test_an_unanswered_registration_says_to_check_the_admin_panel(admin_client, httpx_mock):
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"))

    response = connect(admin_client, "mastodon.example")

    assert response.status_code == 503
    assert b"delete it there before trying again" in response.content


def test_a_bad_address_is_refused_without_calling_the_api_or_using_an_attempt(admin_client, settings):
    settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY = 1

    response = connect(admin_client, "http://mastodon.example")

    assert response.status_code == 400
    assert b"https address" in response.content
    assert social.take_user_attempt(User.objects.get(username="fedi_admin").id)


def test_each_user_has_a_daily_limit(admin_client, httpx_mock, settings):
    settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY = 2
    httpx_mock.add_response(method="POST", url=API, status_code=502, json={"error": "no"}, is_reusable=True)

    statuses = [connect(admin_client, f"server{n}.example").status_code for n in range(3)]

    assert statuses == [502, 502, 429]
    assert len(httpx_mock.get_requests()) == 2


def test_each_server_gets_one_attempt_an_hour_whoever_makes_it(admin_client, client, httpx_mock):
    httpx_mock.add_response(method="POST", url=API, status_code=502, json={"error": "no"})
    connect(admin_client, "mastodon.example")
    client.force_login(make_user("someone_else"))

    response = connect(client, "https://mastodon.example/")

    assert response.status_code == 429
    assert b"in the last hour" in response.content


def test_the_limits_hold_on_redis(redis_cache, settings):
    settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY = 2

    assert [social.take_user_attempt(1) for _ in range(3)] == [True, True, False]
    assert [social.take_host_attempt("mastodon.example") for _ in range(2)] == [True, False]
    assert 0 < redis_cache.ttl(cache.make_key("social:connect:host:mastodon.example")) <= 60 * 60


@pytest.mark.parametrize(
    "live, label",
    [
        ([], "No longer registered with Mwmbl Social"),
        (
            [CREATED | {"status": "active", "capabilities": ["account_search@0.1"]}],
            "Connected. Enabled: account_search@0.1",
        ),
    ],
)
def test_the_list_shows_live_status(admin_client, httpx_mock, live, label):
    stored_registration()
    httpx_mock.add_response(method="GET", url=API, json=live)

    assert label in admin_client.get(CONNECT).content.decode()


def test_the_list_says_when_the_status_is_unavailable(admin_client, httpx_mock):
    stored_registration()
    httpx_mock.add_response(method="GET", url=API, status_code=500)

    response = admin_client.get(CONNECT)

    assert response.status_code == 200
    assert b"Status unavailable" in response.content


def test_users_see_and_disconnect_only_their_own_registrations(admin_client, client, db):
    registration = stored_registration()
    client.force_login(make_user("someone_else"))

    assert CREATED["fingerprint"] not in client.get(CONNECT).content.decode()
    assert client.post(f"{CONNECT}{registration.pk}/disconnect/").status_code == 404
    registration.refresh_from_db()
    assert registration.disconnected_at is None


@pytest.mark.parametrize("api_status", [200, 404])
def test_disconnecting_revokes_the_registration(admin_client, httpx_mock, api_status):
    registration = stored_registration()
    httpx_mock.add_response(method="DELETE", url=f"{API}/4", status_code=api_status, json={})

    response = admin_client.post(f"{CONNECT}{registration.pk}/disconnect/")

    assert response.status_code == 302
    registration.refresh_from_db()
    assert registration.disconnected_at is not None
    assert b"Disconnected" in admin_client.get(CONNECT).content


def test_the_privacy_policy_renders(client, db):
    response = client.get("/social/privacy/")

    assert response.status_code == 200
    assert b"never store non-public posts" in response.content
