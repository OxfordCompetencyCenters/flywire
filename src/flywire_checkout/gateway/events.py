"""Signed event delivery from the gateway to each website.

Every event is POSTed as JSON to the site's ``webhook_url`` with::

    X-Gateway-Event-Id:  evt_...            (stable across retries — use it to de-duplicate)
    X-Gateway-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(webhook_secret, "<t>.<raw body>")>

Sites verify the signature, reject timestamps older than 5 minutes, and answer 2xx.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .sites import SiteConfig

logger = logging.getLogger("flywire_checkout.gateway.events")

SIGNATURE_HEADER = "X-Gateway-Signature"
EVENT_ID_HEADER = "X-Gateway-Event-Id"
SIGNATURE_TOLERANCE_SECONDS = 300


class EventDeliveryError(Exception):
    """The site did not accept the event (non-2xx, timeout, network error)."""


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str | None, body: bytes, *, now: int | None = None,
           tolerance: int = SIGNATURE_TOLERANCE_SECONDS) -> bool:
    """Reference verifier (sites in Python can import this; other languages port it)."""
    if not header:
        return False
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        received = parts["v1"]
    except (ValueError, KeyError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, received)


def event_id_for(*parts: str) -> str:
    """Deterministic id so retries of the same underlying event carry the same id."""
    return "evt_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


class EventForwarder:
    def __init__(self, client: httpx.Client | None = None, timeout_seconds: float = 10.0) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def deliver(self, site: SiteConfig, event_id: str, event_type: str, data: dict[str, Any]) -> bool:
        """Send one event. Returns False when the site has no webhook configured.

        Raises :class:`EventDeliveryError` if the site does not acknowledge it.
        """
        if not site.webhook_url or site.webhook_secret is None:
            return False
        payload = {
            "id": event_id,
            "type": event_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "site_id": site.id,
            "data": data,
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "flywire-checkout-gateway",
            EVENT_ID_HEADER: event_id,
            SIGNATURE_HEADER: sign(site.webhook_secret.get_secret_value(), int(time.time()), body),
        }
        try:
            response = self._client.post(site.webhook_url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise EventDeliveryError(f"{site.id}: {type(exc).__name__}") from exc
        if not response.is_success:
            raise EventDeliveryError(f"{site.id}: HTTP {response.status_code}")
        logger.info("delivered %s %s to site %s", event_type, event_id, site.id)
        return True
