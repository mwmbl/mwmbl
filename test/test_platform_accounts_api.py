"""
Account and submission endpoints of the platform API: registration conflicts, email
confirmation, account deletion, and who may delete or decide a domain submission.
"""

import pytest
from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import DomainEvidence, DomainSubmission

User = get_user_model()

BASE = "/api/v1/platform"
PAGE = {"url": "https://example.org/", "title": "Home", "extract": "Welcome"}


def _user(username: str, verified: bool = True):
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password="correctpassword")
    EmailAddress.objects.create(user=user, email=user.email, verified=verified, primary=True)
    return user


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {RefreshToken.for_user(user).access_token}"}


@pytest.fixture
def owner(db):
    return _user("owner")


@pytest.fixture
def stranger(db):
    return _user("stranger")


@pytest.fixture
def moderator(db):
    user = _user("moderator")
    user.user_permissions.add(Permission.objects.get(codename="change_domain_submission_status"))
    return user


def _register(client, **body):
    payload = {"email": "new@example.com", "password": "correctpassword", **body}
    return client.post(f"{BASE}/register", data=payload, content_type="application/json")


def test_registering_an_existing_email_is_rejected(client, owner):
    response = _register(client, email=owner.email)

    assert response.status_code == 400
    assert "Email already registered" in response.json()["message"]


def test_registering_an_existing_username_is_rejected(client, owner):
    response = _register(client, username=owner.username)

    assert response.status_code == 400
    assert "Username already exists" in response.json()["message"]


def test_registration_records_accepted_agreements(client, db):
    response = _register(client, agreements=["TERMS_OF_SERVICE_GUI"])

    assert response.status_code == 200, response.content


def test_confirming_with_an_unknown_key_is_rejected(client, db):
    response = client.post(
        f"{BASE}/confirm-email",
        data={"email": "new@example.com", "key": "bogus"},
        content_type="application/json",
    )

    assert response.status_code == 400


def test_confirming_with_another_users_email_is_rejected(client, owner):
    unverified = _user("unverified", verified=False)
    address = EmailAddress.objects.get(user=unverified)
    key = EmailConfirmationHMAC(address).key

    response = client.post(
        f"{BASE}/confirm-email",
        data={"email": owner.email, "key": key},
        content_type="application/json",
    )

    assert response.status_code == 400
    address.refresh_from_db()
    assert address.verified is False


def test_confirming_with_the_right_key_verifies_the_address(client, db):
    unverified = _user("unverified", verified=False)
    address = EmailAddress.objects.get(user=unverified)
    key = EmailConfirmationHMAC(address).key

    response = client.post(
        f"{BASE}/confirm-email",
        data={"email": unverified.email, "key": key},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["username"] == "unverified"
    address.refresh_from_db()
    assert address.verified is True


def test_protected_endpoint_accepts_a_verified_user_and_refuses_an_unverified_one(client, owner):
    unverified = _user("unverified", verified=False)

    ok = client.get(f"{BASE}/protected", headers=_auth(owner))
    refused = client.get(f"{BASE}/protected", headers=_auth(unverified))

    assert ok.status_code == 200
    assert refused.status_code == 403


def test_a_user_can_delete_their_own_account(client, owner):
    response = client.delete(f"{BASE}/users/{owner.username}", headers=_auth(owner))

    assert response.status_code == 200
    assert not User.objects.filter(pk=owner.pk).exists()


def test_a_user_cannot_delete_someone_elses_account(client, owner, stranger):
    response = client.delete(f"{BASE}/users/{owner.username}", headers=_auth(stranger))

    assert response.status_code == 400
    assert User.objects.filter(pk=owner.pk).exists()


def test_a_submitter_can_delete_their_own_submission(client, owner):
    submission = DomainSubmission.objects.create(name="example.org", submitted_by=owner)

    response = client.delete(f"{BASE}/domain-submissions/ids/{submission.id}", headers=_auth(owner))

    assert response.status_code == 200
    assert not DomainSubmission.objects.filter(id=submission.id).exists()


def test_a_submission_cannot_be_deleted_by_someone_else(client, owner, stranger):
    submission = DomainSubmission.objects.create(name="example.org", submitted_by=owner)

    response = client.delete(f"{BASE}/domain-submissions/ids/{submission.id}", headers=_auth(stranger))

    assert response.status_code == 400
    assert DomainSubmission.objects.filter(id=submission.id).exists()


def test_only_moderators_can_update_a_submission_status(client, owner, moderator):
    submission = DomainSubmission.objects.create(name="example.org", submitted_by=owner)
    body = {"status": "APPROVED", "rejection_reason": "", "rejection_detail": ""}
    url = f"{BASE}/domain-submissions/ids/{submission.id}"

    refused = client.post(url, data=body, content_type="application/json", headers=_auth(owner))
    accepted = client.post(url, data=body, content_type="application/json", headers=_auth(moderator))

    assert refused.status_code == 400
    assert accepted.status_code == 200
    submission.refresh_from_db()
    assert submission.status == "APPROVED"


def test_submission_detail_is_for_moderators_and_includes_the_evidence(client, owner, moderator):
    submission = DomainSubmission.objects.create(name="example.org", submitted_by=owner)
    DomainEvidence.objects.create(
        domain="example.org",
        state=DomainEvidence.State.READY,
        pages=[PAGE],
    )
    url = f"{BASE}/domain-submissions/ids/{submission.id}"

    refused = client.get(url, headers=_auth(owner))
    detail = client.get(url, headers=_auth(moderator))

    assert refused.status_code in (400, 403)
    assert detail.status_code == 200, detail.content
    assert detail.json()["name"] == "example.org"
    assert detail.json()["evidence_state"] == "READY"
    assert [page["url"] for page in detail.json()["pages"]] == [PAGE["url"]]


def test_detail_for_a_missing_submission_is_a_404(client, moderator):
    response = client.get(f"{BASE}/domain-submissions/ids/999999", headers=_auth(moderator))

    assert response.status_code == 404
