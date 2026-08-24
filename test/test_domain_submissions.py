"""
Tests for domain submission name normalisation: submissions must store a bare
domain, so that the submission list can reverse the "domain" URL for each one.
"""

import importlib

import pytest
from allauth.account.models import EmailAddress
from django.apps import apps as django_apps
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import DomainSubmission, MwmblUser

migration_module = importlib.import_module("mwmbl.migrations.0032_normalize_domain_submission_names")
normalize_names = migration_module.normalize_names


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
def test_api_rejects_invalid_domain(client, access_token):
    response = client.post(
        "/api/v1/platform/domain-submissions/?domain=not-a-domain",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )

    assert response.status_code == 400
    assert DomainSubmission.objects.count() == 0
