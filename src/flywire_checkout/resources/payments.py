"""``/payments`` endpoints (read-only)."""

from __future__ import annotations

import re
from urllib.parse import quote

from ..http import HttpTransport
from ..models.payments import Payment

# Documented formats: ABC123456789 or 1AB12CD452ABC1D; allow a little headroom.
_PAYMENT_ID = re.compile(r"^[A-Za-z0-9]{6,40}$")


def validate_payment_id(payment_id: str) -> str:
    if not isinstance(payment_id, str) or not _PAYMENT_ID.fullmatch(payment_id):
        raise ValueError("Invalid payment reference")
    return payment_id


class Payments:
    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def get(self, payment_id: str) -> Payment:
        """Real-time details of one payment (Flywire: usable as a source of truth, no list latency)."""
        path = f"payments/{quote(validate_payment_id(payment_id), safe='')}"
        return Payment.model_validate(self._transport.request("GET", path).json())
