import base64
import hashlib
import hmac
import json

import httpx
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from flywire_checkout import WebhookVerifier  # noqa: E402
from flywire_checkout.models import CreateCheckoutSessionRequest  # noqa: E402
from flywire_checkout.services import CheckoutService, InMemoryCheckoutStore, NotificationProcessor  # noqa: E402
from flywire_checkout.web import create_app  # noqa: E402

from .conftest import SANDBOX_BASE, SESSION_ID  # noqa: E402


@pytest.fixture
def app_client(client, config, recorder):
    def handler(request):
        if request.url.path.endswith("/checkout/sessions"):
            return httpx.Response(201, json={
                "id": SESSION_ID, "expires_in_seconds": 1800,
                "hosted_form": {"url": "https://elements.flywire.com/v1/form?session_id=x", "method": "GET"},
                "warnings": [{"source": "email"}],
            })
        return httpx.Response(200, json={"payment_reference": "TQQ416812923"})

    recorder.handler = handler
    store = InMemoryCheckoutStore()
    prices = {"order-1": 2500}

    def resolve(owner_id, order_id):
        if order_id not in prices:
            raise LookupError(order_id)
        return CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=prices[order_id], recipient_id="TQQ")

    app = create_app(
        config=config,
        service=CheckoutService(client, store),
        processor=NotificationProcessor(WebhookVerifier(config.shared_secret), store),
        owner_id_provider=lambda request: request.headers.get("X-Test-User", "alice"),
        order_resolver=resolve,
    )
    return TestClient(app)


def test_create_uses_server_side_amount(app_client, recorder):
    res = app_client.post("/api/flywire/checkout-sessions", json={"order_id": "order-1"})
    assert res.status_code == 200
    assert res.json() == {
        "session_id": SESSION_ID, "expires_in_seconds": 1800,
        "hosted_form_url": "https://elements.flywire.com/v1/form?session_id=x",
    }
    assert recorder.json_body()["items"][0]["amount"] == 2500


def test_client_cannot_inject_amount(app_client):
    res = app_client.post("/api/flywire/checkout-sessions", json={"order_id": "order-1", "amount": 1})
    assert res.status_code == 422


def test_unknown_order(app_client):
    assert app_client.post("/api/flywire/checkout-sessions", json={"order_id": "nope"}).status_code == 404


def test_confirm_flow_and_ssrf_guard(app_client, recorder):
    app_client.post("/api/flywire/checkout-sessions", json={"order_id": "order-1"})
    bad = app_client.post("/api/flywire/checkout-sessions/confirm",
                          json={"confirm_url": f"https://evil.example.com/checkout/sessions/{SESSION_ID}/confirm"})
    assert bad.status_code == 400

    other = app_client.post("/api/flywire/checkout-sessions/confirm", headers={"X-Test-User": "mallory"},
                            json={"confirm_url": f"{SANDBOX_BASE}checkout/sessions/{SESSION_ID}/confirm"})
    assert other.status_code == 409

    ok = app_client.post("/api/flywire/checkout-sessions/confirm",
                         json={"confirm_url": f"{SANDBOX_BASE}checkout/sessions/{SESSION_ID}/confirm"})
    assert ok.status_code == 200 and ok.json()["payment_reference"] == "TQQ416812923"
    assert all("evil" not in str(r.url) for r in recorder.requests)


def test_webhook_endpoint(app_client):
    body = json.dumps({"event_type": "initiated", "event_date": "2025-03-31T13:21:27Z",
                       "data": {"payment_id": "TQQ1", "status": "initiated"}}).encode()
    digest = base64.b64encode(hmac.new(b"test-shared-secret", body, hashlib.sha256).digest()).decode()

    assert app_client.post("/webhooks/flywire", content=body, headers={"X-Flywire-Digest": "x"}).status_code == 401
    assert app_client.post("/webhooks/flywire", content=body, headers={"X-Flywire-Digest": digest}).status_code == 200


def test_checkout_page_security_headers(app_client):
    res = app_client.get("/checkout")
    assert res.status_code == 200
    assert "sandbox.main.js" in res.text
    assert "test-api-key" not in res.text and "test-shared-secret" not in res.text
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"


def test_webhook_chunked_oversized_body_rejected(app_client):
    def chunks():
        for _ in range(40):
            yield b"a" * 10_000  # 400 KB, no Content-Length
    res = app_client.post("/webhooks/flywire", content=chunks(), headers={"X-Flywire-Digest": "x"})
    assert res.status_code == 413
