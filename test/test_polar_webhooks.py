import base64
import json
from datetime import datetime, timezone

import pytest
from polar_sdk.webhooks import WebhookVerificationError
from standardwebhooks.webhooks import Webhook

from mwmbl.polar_webhooks import validate_event

SECRET = "whsec_" + base64.b64encode(b"a-thirty-two-byte-signing-key!!!").decode()

BODY = json.dumps(
    {
        "type": "customer.deleted",
        "timestamp": "2026-10-02T00:00:00Z",
        "data": {
            "id": "7a6e5b3c-0000-4000-8000-000000000001",
            "type": "individual",
            "created_at": "2026-10-02T00:00:00Z",
            "modified_at": None,
            "metadata": {},
            "email": "member@example.com",
            "email_verified": True,
            "name": None,
            "billing_name": None,
            "billing_address": None,
            "tax_id": None,
            "organization_id": "7a6e5b3c-0000-4000-8000-000000000002",
            "deleted_at": None,
            "external_id": None,
            "avatar_url": "https://example.com/avatar.png",
        },
    }
)


def _headers(signing_secret):
    timestamp = datetime.now(timezone.utc)
    return {
        "webhook-id": "msg_1",
        "webhook-timestamp": str(int(timestamp.timestamp())),
        "webhook-signature": Webhook(signing_secret).sign("msg_1", timestamp, BODY),
    }


def test_accepts_legacy_signing():
    # Endpoints created before 8 September 2026: the key is the bytes of the whole whsec_ string.
    headers = _headers(base64.b64encode(SECRET.encode()).decode())

    assert validate_event(body=BODY, headers=headers, secret=SECRET).TYPE == "customer.deleted"


def test_accepts_standard_webhooks_signing():
    # Endpoints created since 8 September 2026: the whsec_ secret is the Standard Webhooks key.
    headers = _headers(SECRET)

    assert validate_event(body=BODY, headers=headers, secret=SECRET).TYPE == "customer.deleted"


def test_rejects_wrong_secret():
    headers = _headers("whsec_" + base64.b64encode(b"some-other-key").decode())

    with pytest.raises(WebhookVerificationError):
        validate_event(body=BODY, headers=headers, secret=SECRET)


def test_rejects_tampered_body():
    headers = _headers(SECRET)

    with pytest.raises(WebhookVerificationError):
        validate_event(body=BODY.replace("member@", "attacker@"), headers=headers, secret=SECRET)
