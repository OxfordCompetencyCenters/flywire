"""Flywire Pay-By-Link: JSON Links + callback-based fulfilment.

Reference implementation of the flow Halo (or any system without a payment page of its own)
can use instead of Checkout with API:

1. Server-side, create a **JSON Link** for one request/ticket:
   ``POST https://gateway[.demo].flywire.com/v1/transfers.json`` with header
   ``X-Flywire-Digest = base64(HMAC-SHA256(portal shared secret, exact request body))``.
   The returned link carries a token, so the amount cannot be changed by editing the URL,
   and it expires after one payment (or 1–90 days).
2. The payer pays on Flywire's hosted PayEx portal.
3. Flywire sends signed payment status callbacks (``callback_version`` 2). Verify the
   ``X-Flywire-Digest`` header, then let :class:`FulfilmentPolicy` decide what to do.

Intelligent Links (plain ``?recipient=…&amount=…`` URLs) are deliberately not supported here:
their amount is an ordinary query parameter anyone can edit before paying.

Docs: https://developers.flywire.com/education/Content/pay-by-link-json-links.htm
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .environments import Environment
from .exceptions import AuthenticationError, ConfigurationError, FlywireAPIError, TransportError
from .models.notifications import PaymentNotification, PaymentStatus

logger = logging.getLogger("flywire_checkout.paybylink")

JSON_LINK_ENDPOINTS: dict[Environment, str] = {
    Environment.SANDBOX: "https://gateway.demo.flywire.com/v1/transfers.json",
    Environment.PRODUCTION: "https://gateway.flywire.com/v1/transfers.json",
}

Locale = Literal["en", "es-ES", "zh-CN", "ko-KR", "pt-PT", "ja-JP", "fr-FR", "id-ID", "ar-SA",
                 "vi-VN", "it-IT", "de-DE", "tr-TR"]


def _https(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{field} must be an https:// URL without credentials")
    return value


class JsonLinkRequest(BaseModel):
    """Body of a JSON Link request. Field names match Flywire's API exactly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["PayByJSON"] = "PayByJSON"
    payment_destination: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9-]+$")
    """The portal's subdomain (not the recipient ID). D0226's is ``oxfordtesting``."""

    # Exactly one of amount / items. Amount is required by this library: Flywire accepts a link
    # without it, but on a fixed-amount portal the payer is then stuck at 0.
    amount: int | None = Field(default=None, gt=0, description="Subunits, e.g. 12500 = £125.00")
    items: dict[str, int] | None = None
    """Only for portals configured with items (e.g. {"tuition": 20000})."""
    max_amount: int | None = Field(default=None, gt=0)
    """Lets the payer pay LESS than ``amount``. Leave unset for fixed-price purchases such as licences."""

    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    sender_email: str | None = Field(default=None, max_length=254)
    sender_first_name: str | None = Field(default=None, max_length=100)
    sender_last_name: str | None = Field(default=None, max_length=100)
    sender_address1: str | None = Field(default=None, max_length=255)
    sender_city: str | None = Field(default=None, max_length=100)
    sender_state: str | None = Field(default=None, max_length=50)
    sender_zip: str | None = Field(default=None, max_length=20)
    sender_phone: str | None = Field(default=None, max_length=30)
    allow_to_edit_payer_information: bool | None = None

    dynamic_fields: dict[str, str] | None = None
    """Portal fields, e.g. {"student_first_name": ..., "student_last_name": ..., "student_email": ...}."""
    days_to_expire: int | None = Field(default=None, ge=1, le=90)
    locale: Locale | None = None
    return_cta: str | None = Field(default=None, max_length=500)
    return_cta_name: str | None = Field(default=None, max_length=500)

    callback_url: str | None = Field(default=None, max_length=2000)
    """Dynamic callback URL. With a static portal callback configured, callbacks go to BOTH."""
    callback_id: str = Field(min_length=1, max_length=100)
    """Your reference (e.g. the Halo ticket id). Returned as ``external_reference`` in callbacks.
    Required here: without it a callback cannot be matched to the request it pays for."""
    callback_version: Literal["2"] = "2"

    @field_validator("return_cta")
    @classmethod
    def _return_https(cls, value: str | None) -> str | None:
        return _https(value, "return_cta")

    @field_validator("callback_url")
    @classmethod
    def _callback_https(cls, value: str | None) -> str | None:
        return _https(value, "callback_url")

    @field_validator("items")
    @classmethod
    def _items_positive(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        if value is not None and (not value or any(v < 0 for v in value.values())):
            raise ValueError("items must be non-empty with non-negative amounts")
        return value

    @model_validator(mode="after")
    def _amount_rules(self) -> "JsonLinkRequest":
        if (self.amount is None) == (self.items is None):
            raise ValueError("provide exactly one of amount or items")
        total = self.amount if self.amount is not None else sum(self.items.values())  # type: ignore[union-attr]
        if self.max_amount is not None and total > self.max_amount:
            raise ValueError("amount must not exceed max_amount (Flywire would block the payer)")
        return self

    def to_body(self) -> bytes:
        """The exact bytes that are both signed and sent."""
        payload: dict[str, Any] = self.model_dump(exclude_none=True)
        if "days_to_expire" in payload:
            payload["days_to_expire"] = str(payload["days_to_expire"])  # Flywire documents it as a string
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class JsonLink:
    url: str
    callback_id: str


class PayByLinkError(FlywireAPIError):
    """Flywire refused to create the link (400 malformed JSON, 409 missing parameters, ...)."""


def flywire_digest(shared_secret: str, body: bytes) -> str:
    """``X-Flywire-Digest``: base64(HMAC-SHA256(shared secret, body)). Same scheme as callbacks."""
    return base64.b64encode(hmac.new(shared_secret.encode("utf-8"), body, hashlib.sha256).digest()).decode("ascii")


class PayByLinkClient:
    """Creates JSON Links. Needs the **portal's** shared secret (ask Flywire which secret applies)."""

    def __init__(
        self,
        shared_secret: SecretStr | str,
        environment: Environment = Environment.SANDBOX,
        *,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        secret = shared_secret.get_secret_value() if isinstance(shared_secret, SecretStr) else shared_secret
        if not secret:
            raise ConfigurationError("A Flywire shared secret is required to create JSON Links")
        self._secret = secret
        self.environment = environment
        self.endpoint = JSON_LINK_ENDPOINTS[environment]
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)

    def __repr__(self) -> str:
        return f"PayByLinkClient(environment={self.environment.value!r})"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PayByLinkClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_link(self, request: JsonLinkRequest) -> JsonLink:
        body = request.to_body()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Flywire-Digest": flywire_digest(self._secret, body),
        }
        try:
            response = self._client.post(self.endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TransportError(f"Could not reach Flywire Pay-By-Link: {type(exc).__name__}") from exc

        logger.info("flywire json link -> %s (callback_id=%s)", response.status_code, request.callback_id)
        try:
            data = response.json()
        except ValueError:
            data = None
        errors = data.get("errors") if isinstance(data, dict) else None

        if response.status_code == 401:
            raise AuthenticationError(401, "Unauthorized", "X-Flywire-Digest rejected: wrong shared secret for this portal")
        # Flywire's error bodies ALSO contain a "url" (a generic school-selection page), so a link is
        # only accepted from a clean 200 response — never just because "url" is present.
        if response.status_code != 200 or errors:
            messages = errors if isinstance(errors, list) else []
            detail = None if messages else (
                "Flywire gave no reason; check that JSON Links are enabled for this portal "
                "and that payment_destination is its subdomain"
            )
            raise PayByLinkError(
                response.status_code, "Pay-By-Link request rejected", detail,
                [{"message": str(m)} for m in messages],
            )
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not _is_flywire_payment_url(url):
            raise PayByLinkError(response.status_code, "Unexpected Pay-By-Link response", "no valid payment link returned")
        return JsonLink(url=url, callback_id=request.callback_id)


_REDIRECT_PATH = re.compile(r"^/redirect/[0-9a-fA-F-]{36}$")


def _is_flywire_payment_url(url: str) -> bool:
    """Accept the two link shapes Flywire returns: the documented ``…/orders/<id>?token=…`` and
    (observed live, Oct 2026) ``https://gateway[.demo].flywire.com/redirect/<uuid>``, which
    302-redirects to the former."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return False
    if not (host == "flywire.com" or host.endswith(".flywire.com")):
        return False
    if host.startswith("gateway.") and _REDIRECT_PATH.fullmatch(parts.path) and not parts.query:
        return True
    return parts.path.startswith("/orders/") and "token=" in parts.query


# --------------------------------------------------------------------------- fulfilment


class Decision(str, Enum):
    GRANT = "grant"  # funds guaranteed and they match what was asked for
    WAIT = "wait"  # in progress (initiated, processed, ...) — do nothing yet
    REVOKE = "revoke"  # was paid, now reversed (refund / unpaid direct debit)
    DENY = "deny"  # failed or cancelled — do not grant (note: cancelled can be re-initiated later)
    MISMATCH = "mismatch"  # paid, but amount/currency/reference don't match — needs a human


@dataclass(frozen=True)
class Expectation:
    """What the request/ticket should be paid with."""

    callback_id: str
    amount: int
    currency: str


@dataclass(frozen=True)
class FulfilmentResult:
    decision: Decision
    reason: str


class FulfilmentPolicy:
    """Turns a *verified* Flywire callback into a fulfilment decision for one request.

    Grant on ``guaranteed`` (or ``delivered``) only, and only if the payment is for this
    request (``external_reference`` == ``callback_id``) in the expected amount and currency.
    Callbacks can arrive more than once and out of order: de-duplicate on
    ``notification.idempotency_key`` and ignore events older than the last one applied.
    """

    GRANT_STATUSES = frozenset({PaymentStatus.GUARANTEED, PaymentStatus.DELIVERED})

    def evaluate(self, notification: PaymentNotification, expected: Expectation) -> FulfilmentResult:
        data = notification.data
        status = notification.status
        if (data.external_reference or "") != expected.callback_id:
            return FulfilmentResult(Decision.MISMATCH, "callback is for a different reference")
        if status is PaymentStatus.REVERSED:
            return FulfilmentResult(Decision.REVOKE, "payment reversed")
        if status in (PaymentStatus.FAILED, PaymentStatus.CANCELLED):
            reason = data.cancellation_reason or data.reason or status.value
            return FulfilmentResult(Decision.DENY, f"payment {status.value} ({reason})")
        if status not in self.GRANT_STATUSES:
            return FulfilmentResult(Decision.WAIT, f"payment {status.value if status else notification.event_type}")
        try:
            paid = int(str(data.amount_to))
        except ValueError:
            return FulfilmentResult(Decision.MISMATCH, "amount missing from callback")
        if (data.currency_to or "").upper() != expected.currency.upper():
            return FulfilmentResult(Decision.MISMATCH, f"currency {data.currency_to} != {expected.currency}")
        if paid != expected.amount:
            return FulfilmentResult(Decision.MISMATCH, f"amount {paid} != {expected.amount}")
        return FulfilmentResult(Decision.GRANT, f"payment {status.value}, amount matches")
