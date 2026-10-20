"""
Per-user platform endpoints: agreements, devices, the profile, and the moderator's
re-crawl of a submission.
"""

import pytest
from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import Device, DomainEvidence, DomainSubmission, UserAgreement
from mwmbl.platform import api

User = get_user_model()

BASE = "/api/v1/platform"


def _user(username: str):
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password="correctpassword")
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {RefreshToken.for_user(user).access_token}"}


@pytest.fixture
def member(db):
    return _user("member")


@pytest.fixture
def moderator(db):
    user = _user("moderator")
    user.user_permissions.add(Permission.objects.get(codename="change_domain_submission_status"))
    return user


def test_accepting_an_agreement_records_the_current_version(client, member, settings):
    response = client.post(
        f"{BASE}/agreements/",
        data={"agreement_type": "TERMS_OF_SERVICE_API"},
        content_type="application/json",
        headers=_auth(member),
    )

    assert response.status_code == 200
    assert response.json()["version_id"] == settings.CURRENT_AGREEMENT_VERSIONS["TERMS_OF_SERVICE_API"]
    assert UserAgreement.objects.filter(user=member, agreement_type="TERMS_OF_SERVICE_API").count() == 1


def test_current_agreements_list_the_latest_acceptance_of_each_type(client, member):
    for version in ("v1", "v2"):
        UserAgreement.objects.create(user=member, agreement_type="TERMS_OF_SERVICE_GUI", version_id=version)

    current = client.get(f"{BASE}/agreements/", headers=_auth(member))
    history = client.get(f"{BASE}/agreements/history/", headers=_auth(member))

    assert [(a["agreement_type"], a["version_id"]) for a in current.json()] == [("TERMS_OF_SERVICE_GUI", "v2")]
    assert [a["version_id"] for a in history.json()["items"]] == ["v2", "v1"]


def test_devices_are_listed_newest_first_and_only_for_their_owner(client, member):
    stranger = _user("stranger")
    Device.objects.create(user=member, hostname="old-laptop")
    Device.objects.create(user=member, hostname="new-laptop")
    Device.objects.create(user=stranger, hostname="not-mine")

    response = client.get(f"{BASE}/devices/", headers=_auth(member))

    assert [d["hostname"] for d in response.json()] == ["new-laptop", "old-laptop"]


def test_a_device_can_be_renamed_by_its_owner_only(client, member):
    stranger = _user("stranger")
    device = Device.objects.create(user=member, hostname="laptop")
    url = f"{BASE}/devices/{device.id}"

    refused = client.patch(
        url, data={"friendly_name": "Hijacked"}, content_type="application/json", headers=_auth(stranger)
    )
    renamed = client.patch(
        url, data={"friendly_name": "Office"}, content_type="application/json", headers=_auth(member)
    )

    assert refused.status_code == 404
    assert renamed.status_code == 200
    assert renamed.json()["friendly_name"] == "Office"
    device.refresh_from_db()
    assert device.friendly_name == "Office"


def test_profile_reports_the_account_and_plan(client, member):
    response = client.get(f"{BASE}/user", headers=_auth(member))

    body = response.json()
    assert response.status_code == 200
    assert body["username"] == "member"
    assert body["email"] == "member@example.com"
    assert body["email_confirmed"] is True
    assert body["plan"] == "free"


def _change_username(client, user, username: str):
    return client.put(
        f"{BASE}/user/username", data={"username": username}, content_type="application/json", headers=_auth(user)
    )


def _change_email(client, user, email: str, password: str = "correctpassword"):
    return client.post(
        f"{BASE}/user/email",
        data={"email": email, "password": password},
        content_type="application/json",
        headers=_auth(user),
    )


def test_a_user_can_change_their_username(client, member):
    response = _change_username(client, member, "new_name")

    assert response.status_code == 200
    assert response.json()["username"] == "new_name"
    member.refresh_from_db()
    assert member.username == "new_name"


def test_a_username_can_change_case_alone(client, member):
    response = _change_username(client, member, "Member")

    assert response.status_code == 200
    member.refresh_from_db()
    assert member.username == "Member"


def test_a_case_change_cannot_collide_with_an_existing_case_variant(client, member):
    _user("MEMBER")

    response = _change_username(client, member, "Member")

    assert response.status_code == 400


@pytest.mark.parametrize(
    "username", ["STRANGER", "mwmbl-seed-crawler", "has space", "", "x" * 151, "member@example.com"]
)
def test_taken_reserved_and_invalid_usernames_are_refused(client, member, username):
    _user("stranger")

    response = _change_username(client, member, username)

    assert response.status_code == 400
    member.refresh_from_db()
    assert member.username == "member"


def test_changing_email_needs_the_password(client, member):
    response = _change_email(client, member, "new@example.com", password="wrong")

    assert response.status_code == 403
    assert not EmailAddress.objects.filter(email="new@example.com").exists()


@pytest.mark.parametrize(
    "email", ["stranger@example.com", "STRANGER@example.com", "not-an-email", "member@example.com"]
)
def test_taken_invalid_and_unchanged_emails_are_refused(client, member, email):
    _user("stranger")

    response = _change_email(client, member, email)

    assert response.status_code == 400


def test_a_new_email_replaces_the_old_one_once_confirmed(client, member):
    response = _change_email(client, member, "new@example.com")

    assert response.status_code == 200
    assert mail.outbox[-1].to == ["new@example.com"]
    # The old address keeps working until the new one is confirmed.
    member.refresh_from_db()
    assert member.email == "member@example.com"
    assert client.get(f"{BASE}/protected", headers=_auth(member)).status_code == 200
    assert client.get(f"{BASE}/user", headers=_auth(member)).json()["pending_email"] == "new@example.com"

    pending = EmailAddress.objects.get(email="new@example.com")
    confirmed = client.post(
        f"{BASE}/confirm-email",
        data={"email": "new@example.com", "key": EmailConfirmationHMAC(pending).key},
        content_type="application/json",
    )

    assert confirmed.status_code == 200
    member.refresh_from_db()
    assert member.email == "new@example.com"
    assert list(EmailAddress.objects.filter(user=member).values_list("email", "verified", "primary")) == [
        ("new@example.com", True, True)
    ]


def test_login_moves_to_the_new_email_once_confirmed(client, member):
    _change_email(client, member, "new@example.com")
    pending = EmailAddress.objects.get(email="new@example.com")
    client.post(
        f"{BASE}/confirm-email",
        data={"email": "new@example.com", "key": EmailConfirmationHMAC(pending).key},
        content_type="application/json",
    )

    def log_in(username):
        return client.post(
            f"{BASE}/token/pair",
            data={"username": username, "password": "correctpassword"},
            content_type="application/json",
        )

    assert log_in("new@example.com").status_code == 200
    assert log_in("member@example.com").status_code == 401


def test_an_email_pending_on_another_account_cannot_be_registered(client, member):
    _change_email(client, member, "new@example.com")

    response = client.post(
        f"{BASE}/register",
        data={"email": "NEW@example.com", "password": "anotherpassword"},
        content_type="application/json",
    )

    assert response.status_code == 400


def test_confirming_an_email_another_account_has_verified_is_refused(client, member):
    _change_email(client, member, "new@example.com")
    pending = EmailAddress.objects.get(email="new@example.com")
    stranger = _user("stranger")
    EmailAddress.objects.create(user=stranger, email="new@example.com", verified=True)

    response = client.post(
        f"{BASE}/confirm-email",
        data={"email": "new@example.com", "key": EmailConfirmationHMAC(pending).key},
        content_type="application/json",
    )

    assert response.status_code == 400
    member.refresh_from_db()
    assert member.email == "member@example.com"


def test_a_second_email_change_replaces_the_pending_one(client, member):
    _change_email(client, member, "first@example.com")
    _change_email(client, member, "second@example.com")

    assert sorted(EmailAddress.objects.filter(user=member).values_list("email", flat=True)) == [
        "member@example.com",
        "second@example.com",
    ]


def test_refetching_clears_the_old_evidence_and_queues_a_crawl(client, member, moderator, monkeypatch):
    submission = DomainSubmission.objects.create(name="example.org", submitted_by=member)
    DomainEvidence.objects.create(domain="example.org", state=DomainEvidence.State.READY)
    queued = []
    monkeypatch.setattr(api, "enrich_domain_submission", queued.append)
    url = f"{BASE}/domain-submissions/ids/{submission.id}/refetch"

    refused = client.post(url, headers=_auth(member))
    accepted = client.post(url, headers=_auth(moderator))

    assert refused.status_code == 403
    assert accepted.status_code == 200
    assert queued == ["example.org"]
    assert not DomainEvidence.objects.filter(domain="example.org").exists()


def test_refetching_a_missing_submission_is_a_404(client, moderator):
    response = client.post(f"{BASE}/domain-submissions/ids/999999/refetch", headers=_auth(moderator))

    assert response.status_code == 404


def test_index_stats_are_empty_when_redis_cannot_be_read(monkeypatch):
    def unreachable(domain):
        raise ConnectionError("redis down")

    monkeypatch.setattr(api.stats_manager, "get_stats_for_domain", unreachable)

    assert api._index_stats("example.org") == {}
