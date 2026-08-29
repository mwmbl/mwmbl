"""
Tests for domain submission name normalisation: submissions must store a bare
domain, so that the submission list can reverse the "domain" URL for each one.
"""

import importlib
import json

import pytest
from allauth.account.models import EmailAddress
from django.apps import apps as django_apps
from django.contrib.auth.models import Permission
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import DomainSubmission, MwmblUser

normalize_names = importlib.import_module(
    "mwmbl.migrations.0032_normalize_domain_submission_names").normalize_names
fix_names = importlib.import_module("mwmbl.migrations.0033_fix_domain_submission_names").fix_names


@pytest.fixture
def user(db):
    return MwmblUser.objects.create_user(username="submitter", email="submitter@example.com", password="password")


@pytest.fixture
def verified_user(user):
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user


@pytest.fixture
def access_token(verified_user):
    return str(RefreshToken.for_user(verified_user).access_token)


@pytest.mark.django_db
def test_migration_normalizes_urls(user):
    submission = DomainSubmission.objects.create(name="https://www.idolbronze.com/", submitted_by=user)
    unchanged = DomainSubmission.objects.create(name="example.com", submitted_by=user)

    normalize_names(django_apps, None)

    submission.refresh_from_db()
    unchanged.refresh_from_db()
    assert submission.name == "www.idolbronze.com"
    assert unchanged.name == "example.com"


@pytest.mark.django_db
def test_submission_list_renders_after_migration(client, user):
    DomainSubmission.objects.create(name="https://www.idolbronze.com/", submitted_by=user)

    normalize_names(django_apps, None)

    response = client.get(reverse("domain_submissions"))
    assert response.status_code == 200
    assert reverse("domain", args=["www.idolbronze.com"]) in response.content.decode()


@pytest.mark.django_db
def test_form_stores_bare_domain(client, user):
    client.force_login(user)
    response = client.post(reverse("submit_domain"), {"name": "https://www.idolbronze.com/"})

    assert response.status_code == 302
    assert DomainSubmission.objects.get().name == "www.idolbronze.com"


@pytest.mark.django_db
def test_api_stores_bare_domain(client, access_token):
    response = client.post(
        "/api/v1/platform/domain-submissions/?domain=https://www.idolbronze.com/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 200
    assert DomainSubmission.objects.get().name == "www.idolbronze.com"


@pytest.mark.django_db
def test_fix_migration_lowercases_names(user):
    submission = DomainSubmission.objects.create(name="WWW.IdolBronze.com", submitted_by=user)

    fix_names(django_apps, None)

    submission.refresh_from_db()
    assert submission.name == "www.idolbronze.com"


@pytest.mark.django_db
def test_fix_migration_deletes_names_that_are_not_domains(user):
    DomainSubmission.objects.create(name="http://", submitted_by=user)
    DomainSubmission.objects.create(name="https:///some/path", submitted_by=user)
    kept = DomainSubmission.objects.create(name="example.com", submitted_by=user)

    fix_names(django_apps, None)

    assert list(DomainSubmission.objects.all()) == [kept]


@pytest.mark.django_db
def test_submission_list_renders_after_fix_migration(client, user):
    DomainSubmission.objects.create(name="http://", submitted_by=user)

    fix_names(django_apps, None)

    response = client.get(reverse("domain_submissions"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_api_finds_submission_whatever_the_case(client, access_token):
    client.post(
        "/api/v1/platform/domain-submissions/?domain=https://WWW.IdolBronze.com/",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    response = client.get("/api/v1/platform/domain-submissions/domains/WWW.IdolBronze.com")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["www.idolbronze.com"]


@pytest.mark.django_db
def test_api_rejects_invalid_domain(client, access_token):
    response = client.post(
        "/api/v1/platform/domain-submissions/?domain=not-a-domain",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 400
    assert DomainSubmission.objects.count() == 0


@pytest.mark.django_db
def test_api_update_submission_status_grants_moderator_permission(client, verified_user, access_token):
    """A user with the change_domain_submission_status permission can moderate via the API.

    Regression test: the endpoint checked `has_perm("change_domain_submission_status")`
    without the `mwmbl.` app label, which Django never matches (permissions are stored
    as `app_label.codename`), so moderation always failed with 400.
    """
    submission = DomainSubmission.objects.create(name="example.com", submitted_by=verified_user)
    permission = Permission.objects.get(
        codename="change_domain_submission_status",
        content_type__app_label="mwmbl",
    )
    verified_user.user_permissions.add(permission)

    response = client.post(
        f"/api/v1/platform/domain-submissions/ids/{submission.id}",
        data=json.dumps({"status": "APPROVED", "rejection_reason": "", "rejection_detail": ""}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 200
    submission.refresh_from_db()
    assert submission.status == "APPROVED"


@pytest.mark.django_db
def test_api_update_status_rejects_a_moderation_action_as_a_status(client, verified_user, access_token):
    """"APPROVE" is the word a client shows its moderator; "APPROVED" is the status.

    Posting the former used to be accepted and written straight to the column - `choices` is
    checked by full_clean(), which save() never calls - and the submission then matched
    neither the pending queue nor the approved set, so the decision vanished. It is a 422
    naming the field now.
    """
    submission = DomainSubmission.objects.create(name="example.com", submitted_by=verified_user)
    verified_user.user_permissions.add(Permission.objects.get(
        codename="change_domain_submission_status", content_type__app_label="mwmbl"))

    response = client.post(
        f"/api/v1/platform/domain-submissions/ids/{submission.id}",
        data=json.dumps({"status": "APPROVE", "rejection_reason": "", "rejection_detail": ""}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 422
    assert "status must be one of" in response.content.decode()
    submission.refresh_from_db()
    assert submission.status == "PENDING"


@pytest.mark.django_db
def test_api_update_status_accepts_an_approval_without_rejection_fields(client, verified_user, access_token):
    """Both rejection columns are NOT NULL, and ninja types them Optional, so omitting them
    on an approval sent None into the column and came back as a 500."""
    submission = DomainSubmission.objects.create(name="example.com", submitted_by=verified_user)
    verified_user.user_permissions.add(Permission.objects.get(
        codename="change_domain_submission_status", content_type__app_label="mwmbl"))

    response = client.post(
        f"/api/v1/platform/domain-submissions/ids/{submission.id}",
        data=json.dumps({"status": "APPROVED"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 200
    submission.refresh_from_db()
    assert submission.status == "APPROVED"
    assert submission.rejection_reason == ""
    assert submission.rejection_detail == ""
