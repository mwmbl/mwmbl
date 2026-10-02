"""Verify Polar webhook deliveries signed with either of Polar's two signing keys."""

from polar_sdk._webhooks import _KNOWN_EVENT_TYPES, WebhookPayloadAdapter
from polar_sdk.webhooks import WebhookUnknownTypeError, WebhookVerificationError, WebhoookPayload
from polar_sdk.webhooks import validate_event as validate_legacy_event
from standardwebhooks.webhooks import Webhook
from standardwebhooks.webhooks import WebhookVerificationError as StandardWebhookVerificationError


def validate_event(body: str | bytes, headers: dict[str, str], secret: str) -> WebhoookPayload:
    """Verify and parse a Polar webhook delivery.

    Endpoints created before 8 September 2026 sign with the UTF-8 bytes of the whole `whsec_…`
    secret, which is what the pinned SDK's validate_event checks. Later endpoints follow the
    Standard Webhooks spec and sign with the secret as-is. Polar's SDKs from 1.0.0-alpha.19 try
    both; this does the same for the 0.x SDK we pin.
    """
    try:
        return validate_legacy_event(body=body, headers=headers, secret=secret)
    except WebhookVerificationError:
        try:
            data = Webhook(secret).verify(body, headers)
        except (StandardWebhookVerificationError, ValueError) as e:
            # ValueError covers a legacy secret that isn't valid base64 once its prefix is stripped.
            raise WebhookVerificationError(str(e)) from e

    if data.get("type") not in _KNOWN_EVENT_TYPES:
        raise WebhookUnknownTypeError(data.get("type"))
    return WebhookPayloadAdapter.validate_python(data)
