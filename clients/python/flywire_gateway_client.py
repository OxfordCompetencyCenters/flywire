"""Drop-in client for the Flywire Payment Gateway. Standard library only (Python 3.9+).

Copy this file into your website (Django, Flask, FastAPI, ...).

    from flywire_gateway_client import GatewayClient, verify_webhook

    gateway = GatewayClient("https://payments.example.ac.uk", os.environ["PAYMENT_GATEWAY_API_KEY"])
    checkout = gateway.create_checkout(order_reference="INV-1001", amount=12500,
                                       recipient_fields={"student_email": "s@example.ac.uk", ...})
    redirect(checkout["payment_url"])

    # webhook view:
    if not verify_webhook(os.environ["PAYMENT_GATEWAY_WEBHOOK_SECRET"],
                          request.headers.get("X-Gateway-Signature"), request.body):
        return HttpResponse(status=400)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

SIGNATURE_TOLERANCE_SECONDS = 300


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.details = status, code, message, details


class GatewayClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0) -> None:
        if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("base_url must be https://")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"GatewayClient({self.base_url!r})"

    def create_checkout(
        self,
        order_reference: str,
        amount: int,
        *,
        description: Optional[str] = None,
        payer: Optional[Dict[str, str]] = None,
        recipient_fields: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, str]] = None,
        session_type: str = "one_off",
    ) -> Dict[str, Any]:
        """Create (or fetch, if order_reference was used before) a checkout. ``amount`` is in subunits."""
        body: Dict[str, Any] = {"order_reference": order_reference, "amount": amount, "session_type": session_type}
        for key, value in (("description", description), ("payer", payer),
                           ("recipient_fields", recipient_fields), ("metadata", metadata)):
            if value:
                body[key] = value
        return self._request("POST", "/v1/checkouts", body)

    def get_checkout(self, checkout_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v1/checkouts/{urllib.parse.quote(checkout_id, safe='')}")

    def cancel_checkout(self, checkout_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/v1/checkouts/{urllib.parse.quote(checkout_id, safe='')}/cancel", {})

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        # Scheme is restricted to https (or http://localhost) in __init__.
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers={  # noqa: S310
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read()).get("error", {})
            except ValueError:
                error = {}
            raise GatewayError(exc.code, error.get("code", "http_error"), error.get("message", exc.reason),
                               error.get("details")) from None


def verify_webhook(secret: str, signature_header: Optional[str], raw_body: bytes,
                   tolerance: int = SIGNATURE_TOLERANCE_SECONDS) -> bool:
    """Check ``X-Gateway-Signature: t=<unix>,v1=<hex>`` over the exact raw request body."""
    if not signature_header or not isinstance(raw_body, (bytes, bytearray)):
        return False
    try:
        parts = dict(item.split("=", 1) for item in signature_header.split(","))
        timestamp, received = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + bytes(raw_body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
