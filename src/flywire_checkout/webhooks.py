"""Verification and parsing of Flywire callbacks (webhooks).

Flywire signs each notification with ``X-Flywire-Digest``:
``base64( HMAC-SHA256(key=shared_secret, msg=raw_http_body) )``.
The digest must be computed over the exact raw bytes received — never re-serialised JSON.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from pydantic import SecretStr, ValidationError

from .exceptions import ConfigurationError, SignatureVerificationError
from .models.notifications import PaymentNotification

DIGEST_HEADER = "X-Flywire-Digest"
MAX_BODY_BYTES = 256 * 1024


class WebhookVerifier:
    def __init__(self, shared_secret: SecretStr | str | None) -> None:
        secret = shared_secret.get_secret_value() if isinstance(shared_secret, SecretStr) else shared_secret
        if not secret:
            raise ConfigurationError("A Flywire shared secret is required to verify notifications")
        self._key = secret.encode("utf-8")

    def __repr__(self) -> str:
        return "WebhookVerifier(shared_secret=**********)"

    def compute_digest(self, raw_body: bytes) -> str:
        mac = hmac.new(self._key, raw_body, hashlib.sha256).digest()
        return base64.b64encode(mac).decode("ascii")

    def verify(self, raw_body: bytes, digest_header: str | None) -> None:
        if not isinstance(raw_body, (bytes, bytearray)):
            raise TypeError("raw_body must be the raw request bytes")
        if len(raw_body) > MAX_BODY_BYTES:
            raise SignatureVerificationError("Notification body too large")
        if not digest_header or not digest_header.strip():
            raise SignatureVerificationError(f"Missing {DIGEST_HEADER} header")
        expected = self.compute_digest(bytes(raw_body)).encode("ascii")
        # Constant-time comparison to avoid timing side channels.
        if not hmac.compare_digest(expected, digest_header.strip().encode("ascii", "ignore")):
            raise SignatureVerificationError("Notification digest mismatch")

    def parse_payment_notification(self, raw_body: bytes, digest_header: str | None) -> PaymentNotification:
        """Verify first, then parse. Never touch the payload before verification succeeds."""
        self.verify(raw_body, digest_header)
        try:
            payload = json.loads(raw_body)
            return PaymentNotification.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            # Signed by Flywire but not in a shape we understand — surface it, don't swallow it.
            raise ValueError(f"Unrecognised Flywire notification payload: {type(exc).__name__}") from exc
