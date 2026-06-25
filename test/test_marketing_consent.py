import json

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import MarketingConsent, MarketingSource
from mwmbl.platform.api import make_unsubscribe_token

User = get_user_model()

REGISTER_URL = "/api/v1/platform/register"
CONSENT_URL = "/api/v1/platform/marketing-consent"
UNSUBSCRIBE_URL = "/api/v1/platform/marketing/unsubscribe"


@pytest.fixture
def verified_user(db):
    user = User.objects.create_user(
        username="swift_falcon_379",
        email="user@example.com",
        password="correctpassword",
    )
    EmailAddress.objects.create(
        user=user, email="user@example.com", verified=True, primary=True
    )
    return user


@pytest.fixture
def access_token(verified_user):
    return str(RefreshToken.for_user(verified_user).access_token)


def _register(client, **extra):
    body = {"email": "new@example.com", "password": "correctpassword"}
    body.update(extra)
    return client.post(REGISTER_URL, data=json.dumps(body), content_type="application/json")


# ---------------------------------------------------------------------------
# Registration records consent
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_register_with_opt_in_records_consent(client):
    response = _register(client, source="API", marketing_opt_in=True)
    assert response.status_code == 200
    user = User.objects.get(email="new@example.com")
    consents = MarketingConsent.objects.filter(user=user)
    assert consents.count() == 1
    consent = consents.first()
    assert consent.source == MarketingSource.API
    assert consent.opted_in is True


@pytest.mark.django_db
def test_register_with_decline_still_records(client):
    response = _register(client, source="GUI", marketing_opt_in=False)
    assert response.status_code == 200
    user = User.objects.get(email="new@example.com")
    consent = MarketingConsent.objects.get(user=user)
    assert consent.source == MarketingSource.GUI
    assert consent.opted_in is False


@pytest.mark.django_db
def test_register_without_source_records_nothing(client):
    response = _register(client, marketing_opt_in=True)
    assert response.status_code == 200
    user = User.objects.get(email="new@example.com")
    assert MarketingConsent.objects.filter(user=user).count() == 0


# ---------------------------------------------------------------------------
# Authenticated view + update
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_get_marketing_consent_returns_latest_per_source(client, verified_user, access_token):
    MarketingConsent.objects.create(user=verified_user, source=MarketingSource.API, opted_in=True)
    MarketingConsent.objects.create(user=verified_user, source=MarketingSource.API, opted_in=False)

    response = client.get(CONSENT_URL, HTTP_AUTHORIZATION=f"Bearer {access_token}")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["source"] == "API"
    assert data[0]["opted_in"] is False


@pytest.mark.django_db
def test_update_marketing_consent_appends_row(client, verified_user, access_token):
    MarketingConsent.objects.create(user=verified_user, source=MarketingSource.GUI, opted_in=True)

    response = client.post(
        CONSENT_URL,
        data=json.dumps({"source": "GUI", "opted_in": False}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
    )
    assert response.status_code == 200
    assert response.json()["opted_in"] is False
    # History preserved — row count grows rather than mutating in place.
    assert MarketingConsent.objects.filter(user=verified_user, source=MarketingSource.GUI).count() == 2

    # GET now reflects the withdrawal.
    latest = client.get(CONSENT_URL, HTTP_AUTHORIZATION=f"Bearer {access_token}").json()
    assert latest[0]["opted_in"] is False


@pytest.mark.django_db
def test_marketing_consent_requires_auth(client):
    assert client.get(CONSENT_URL).status_code == 401


# ---------------------------------------------------------------------------
# One-click unsubscribe (RFC 8058)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_unsubscribe_token_roundtrip_records_opt_out(client, verified_user):
    token = make_unsubscribe_token(verified_user, MarketingSource.API)
    response = client.post(f"{UNSUBSCRIBE_URL}?token={token}")
    assert response.status_code == 200
    consent = MarketingConsent.objects.get(user=verified_user, source=MarketingSource.API)
    assert consent.opted_in is False


@pytest.mark.django_db
def test_unsubscribe_is_idempotent(client, verified_user):
    token = make_unsubscribe_token(verified_user, MarketingSource.GUI)
    client.post(f"{UNSUBSCRIBE_URL}?token={token}")
    client.post(f"{UNSUBSCRIBE_URL}?token={token}")
    consents = MarketingConsent.objects.filter(user=verified_user, source=MarketingSource.GUI)
    assert consents.count() == 2
    assert all(c.opted_in is False for c in consents)


@pytest.mark.django_db
def test_unsubscribe_rejects_bad_token(client, verified_user):
    response = client.post(f"{UNSUBSCRIBE_URL}?token=garbage")
    assert response.status_code == 400
    assert MarketingConsent.objects.count() == 0


@pytest.mark.django_db
def test_unsubscribe_get_not_allowed(client, verified_user):
    # The API only exposes a POST endpoint; the browser-facing unsubscribe page
    # lives in the front-end (mwmbl.org), which calls this POST via JS. A GET
    # (e.g. from a link prefetcher or scanner) must never record an opt-out.
    token = make_unsubscribe_token(verified_user, MarketingSource.API)
    response = client.get(f"{UNSUBSCRIBE_URL}?token={token}")
    assert response.status_code == 405
    assert MarketingConsent.objects.filter(user=verified_user).count() == 0
