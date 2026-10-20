import base64
import hashlib
import hmac
import json

import httpx
import pytest

from flywire_checkout import CheckoutStateError, SignatureVerificationError, WebhookVerifier
from flywire_checkout.models import CreateCheckoutSessionRequest, PaymentStatus
from flywire_checkout.services import (
    CheckoutService,
    InMemoryCheckoutStore,
    NotificationProcessor,
    ProcessingOutcome,
    SessionStatus,
)

from .conftest import SANDBOX_BASE, SESSION_ID

SECRET = "test-shared-secret"
NOTIFICATION = {
    "event_type": "guaranteed",
    "event_date": "2021-05-20T11:25:05Z",
    "event_resource": "payments",
    "data": {
        "payment_id": "PTU146221637", "amount_from": "4225", "currency_from": "EUR", "amount_to": "5000",
        "currency_to": "USD", "status": "guaranteed", "external_reference": "a-reference", "country": "ES",
        "payment_method": {"type": "card", "brand": "visa"}, "fields": {"student_id": "ID123456"},
    },
}


def sign(body: bytes, secret: str = SECRET) -> str:
    # Independent re-implementation of Flywire's documented algorithm.
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# ---------------------------------------------------------------- verifier


def test_verifier_matches_documented_algorithm():
    body = json.dumps(NOTIFICATION).encode()
    verifier = WebhookVerifier(SECRET)
    assert verifier.compute_digest(body) == sign(body)
    note = verifier.parse_payment_notification(body, sign(body))
    assert note.status is PaymentStatus.GUARANTEED
    assert note.data.payment_id == "PTU146221637"


@pytest.mark.parametrize("header", [None, "", "bm90LXRoZS1kaWdlc3Q=", "garbage ✓"])
def test_verifier_rejects_bad_digest(header):
    with pytest.raises(SignatureVerificationError):
        WebhookVerifier(SECRET).verify(b"{}", header)


def test_verifier_rejects_tampered_body():
    body = json.dumps(NOTIFICATION).encode()
    tampered = body.replace(b"5000", b"9999")
    with pytest.raises(SignatureVerificationError):
        WebhookVerifier(SECRET).verify(tampered, sign(body))


def test_verifier_rejects_wrong_secret():
    body = b'{"a":1}'
    with pytest.raises(SignatureVerificationError):
        WebhookVerifier(SECRET).verify(body, sign(body, "other-secret"))


# ---------------------------------------------------------------- notification processor


@pytest.fixture
def processor():
    return NotificationProcessor(WebhookVerifier(SECRET), InMemoryCheckoutStore())


def test_processor_dispatches_once(processor):
    seen = []
    processor.on("guaranteed")(seen.append)
    body = json.dumps(NOTIFICATION).encode()

    first = processor.process(body, sign(body))
    retry = processor.process(body, sign(body))  # Flywire re-delivers

    assert first.outcome is ProcessingOutcome.PROCESSED and first.http_status == 200
    assert retry.outcome is ProcessingOutcome.DUPLICATE and retry.http_status == 200
    assert len(seen) == 1


def test_processor_failed_handler_allows_redelivery(processor):
    calls = {"n": 0}

    @processor.on()
    def flaky(_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")

    body = json.dumps(NOTIFICATION).encode()
    assert processor.process(body, sign(body)).http_status == 500
    assert processor.process(body, sign(body)).outcome is ProcessingOutcome.PROCESSED


def test_processor_rejects_unsigned(processor):
    body = json.dumps(NOTIFICATION).encode()
    result = processor.process(body, "forged")
    assert result.outcome is ProcessingOutcome.REJECTED and result.http_status == 401


# ---------------------------------------------------------------- checkout service


@pytest.fixture
def service(client, recorder):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/checkout/sessions"):
            return httpx.Response(201, json={"id": SESSION_ID, "expires_in_seconds": 1800, "warnings": []})
        if request.url.path.endswith("/confirm"):
            return httpx.Response(200, json={"payment_reference": "TQQ416812923"})
        return httpx.Response(204)

    recorder.handler = handler
    store = InMemoryCheckoutStore()
    return CheckoutService(client, store), store


CONFIRM_URL = f"{SANDBOX_BASE}checkout/sessions/{SESSION_ID}/confirm"


def test_full_flow_and_single_confirm(service, recorder):
    svc, store = service
    svc.create_session("alice", CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=100, recipient_id="TQQ"))
    result = svc.confirm_session("alice", CONFIRM_URL)

    assert result.payment_reference == "TQQ416812923"
    assert store.get_session(SESSION_ID).status is SessionStatus.CONFIRMED
    with pytest.raises(CheckoutStateError):
        svc.confirm_session("alice", CONFIRM_URL)
    assert sum(r.url.path.endswith("/confirm") for r in recorder.requests) == 1


def test_other_user_cannot_confirm_or_expire(service, recorder):
    svc, _ = service
    svc.create_session("alice", CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=100, recipient_id="TQQ"))
    with pytest.raises(CheckoutStateError, match="Unknown"):
        svc.confirm_session("mallory", CONFIRM_URL)
    with pytest.raises(CheckoutStateError, match="Unknown"):
        svc.expire_session("mallory", SESSION_ID)
    assert not any(r.url.path.endswith(("/confirm", "/expire")) for r in recorder.requests)


def test_default_notifications_url_applied(recorder):
    from flywire_checkout import FlywireClient, FlywireConfig

    cfg = FlywireConfig(api_key="k", notifications_url="https://shop.example.com/webhooks/flywire")
    recorder.handler = lambda r: httpx.Response(201, json={"id": SESSION_ID})
    flywire = FlywireClient(cfg, http_client=httpx.Client(transport=httpx.MockTransport(recorder)))
    CheckoutService(flywire, InMemoryCheckoutStore()).create_session(
        "alice", CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=1, recipient_id="TQQ")
    )
    assert recorder.json_body()["notifications_url"] == "https://shop.example.com/webhooks/flywire"


def test_webhook_updates_session_payment_status(service):
    svc, store = service
    svc.create_session("alice", CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=100, recipient_id="TQQ"))
    svc.confirm_session("alice", CONFIRM_URL)

    payload = json.loads(json.dumps(NOTIFICATION))
    payload["data"]["payment_id"] = "TQQ416812923"
    body = json.dumps(payload).encode()
    NotificationProcessor(WebhookVerifier(SECRET), store).process(body, sign(body))
    assert store.get_session(SESSION_ID).payment_status == "guaranteed"


def _deliverer(svc, store):
    svc.create_session("alice", CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=100, recipient_id="TQQ"))
    svc.confirm_session("alice", CONFIRM_URL)
    processor = NotificationProcessor(WebhookVerifier(SECRET), store)

    def deliver(status, date):
        payload = json.loads(json.dumps(NOTIFICATION))
        payload.update(event_type=status, event_date=date)
        payload["data"].update(payment_id="TQQ416812923", status=status)
        body = json.dumps(payload).encode()
        return processor.process(body, sign(body))

    return deliver


def test_stale_retry_does_not_overwrite_newer_status(service):
    svc, store = service
    deliver = _deliverer(svc, store)
    deliver("delivered", "2026-01-03T00:00:00Z")
    late = deliver("guaranteed", "2026-01-02T00:00:00Z")  # older event, retried late
    assert late.outcome is ProcessingOutcome.PROCESSED  # handlers still see it
    assert store.get_session(SESSION_ID).payment_status == "delivered"


def test_documented_cancelled_back_to_processed_flow(service):
    # Flywire magic value SANDBOX_CANCELLED_BACK_TO_PROCESS_STATUS:
    # Initiated -> Cancelled -> Initiated -> Processed -> Guaranteed -> Delivered
    svc, store = service
    deliver = _deliverer(svc, store)
    flow = ["initiated", "cancelled", "initiated", "processed", "guaranteed", "delivered"]
    for minute, status in enumerate(flow):
        deliver(status, f"2026-01-01T00:{minute:02d}:00Z")
        assert store.get_session(SESSION_ID).payment_status == status
