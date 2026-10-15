"""
Per-user platform endpoints: agreements, devices, the profile, and the moderator's
re-crawl of a submission.
"""

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
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
