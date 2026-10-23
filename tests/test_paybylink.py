import base64
import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from flywire_checkout import AuthenticationError, Environment, TransportError, WebhookVerifier
from flywire_checkout.paybylink import (
    Decision,
    Expectation,
    FulfilmentPolicy,
    JsonLinkRequest,
    PayByLinkClient,
    PayByLinkError,
    flywire_digest,
)

SECRET = "portal-shared-secret"
LINK = "https://payment.demo.flywire.com/orders/4bee6c2c-ae10-465c-ba84-1a978e11681a?token=5e03c2cb-2877-47d8-8226-fbfbf62361b3"
FIELDS = {"student_first_name": "Ada", "student_last_name": "Lovelace", "student_email": "ada@example.ac.uk"}


def licence_request(**overrides):
    base = dict(payment_destination="oxfordtesting", amount=12500, callback_id="HALO-1234",
                dynamic_fields=FIELDS, sender_email="ada@example.ac.uk", country="GB",
                callback_url="https://oxford.haloitsm.com/api/flywire", days_to_expire=7,
                return_cta="https://oxford.haloitsm.com/portal", locale="en")
    return JsonLinkRequest(**{**base, **overrides})


def client_with(handler, env=Environment.SANDBOX):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    return PayByLinkClient(SECRET, env, http_client=httpx.Client(transport=httpx.MockTransport(wrapped))), seen


# --------------------------------------------------------------------------- request


def test_body_matches_flywire_contract():
    body = json.loads(licence_request().to_body())
    assert body["provider"] == "PayByJSON" and body["payment_destination"] == "oxfordtesting"
    assert body["amount"] == 12500 and "max_amount" not in body
    assert body["dynamic_fields"] == FIELDS
    assert body["callback_id"] == "HALO-1234" and body["callback_version"] == "2"
    assert body["days_to_expire"] == "7"  # documented as a string
    assert "items" not in body and "allow_to_edit_payer_information" not in body


@pytest.mark.parametrize("overrides", [
    {"amount": None},                                   # would leave a fixed-amount portal stuck at 0
    {"amount": 0},
    {"items": {"tuition": 100}},                        # both amount and items
    {"amount": 12500, "max_amount": 100},               # amount > max_amount blocks the payer
    {"callback_id": ""},
    {"callback_url": "http://insecure.example.com"},
    {"return_cta": "javascript:alert(1)"},
    {"days_to_expire": 91},
    {"payment_destination": "bad host/name"},
    {"provider": "PayByLink"},
    {"locale": "xx"},
    {"amount_typo": 1},
])
def test_invalid_requests_rejected(overrides):
    with pytest.raises(ValidationError):
        licence_request(**overrides)


def test_items_portal_supported():
    body = json.loads(licence_request(amount=None, items={"tuition": 20000, "housing": 0}).to_body())
    assert body["items"] == {"tuition": 20000, "housing": 0} and "amount" not in body


# --------------------------------------------------------------------------- client


def test_create_link_signs_exact_bytes_sent():
    client, seen = client_with(lambda r: httpx.Response(200, json={"url": LINK}))
    link = client.create_link(licence_request())
    sent = seen[0]
    assert str(sent.url) == "https://gateway.demo.flywire.com/v1/transfers.json"
    expected = base64.b64encode(hmac.new(SECRET.encode(), sent.content, hashlib.sha256).digest()).decode()
    assert sent.headers["X-Flywire-Digest"] == expected  # independent re-implementation
    assert link.url == LINK and link.callback_id == "HALO-1234"


REDIRECT_LINK = "https://gateway.demo.flywire.com/redirect/0e71e21d-b74a-44d4-be39-6409fb34dbac"


def test_live_redirect_link_shape_accepted():
    # Shape returned by Flywire's demo JSON Link API on 23 Oct 2026 (302s to /orders/<id>?token=…).
    client, _ = client_with(lambda r: httpx.Response(200, json={"url": REDIRECT_LINK}))
    assert client.create_link(licence_request()).url == REDIRECT_LINK


def test_production_endpoint():
    client, seen = client_with(lambda r: httpx.Response(200, json={"url": LINK.replace(".demo", "")}),
                               env=Environment.PRODUCTION)
    client.create_link(licence_request())
    assert str(seen[0].url) == "https://gateway.flywire.com/v1/transfers.json"


@pytest.mark.parametrize("status, body", [
    (400, {"errors": ["Malformed JSON"], "url": "https://www.demo.flywire.com/select-school"}),
    (409, {"errors": ["Payment destination can't be blank"], "url": "https://www.demo.flywire.com/select-school"}),
    (200, {"errors": ["Provider can't be blank"], "url": "https://payment.demo.flywire.com?destination=TQQ"}),
])
def test_error_bodies_with_url_are_not_treated_as_links(status, body):
    client, _ = client_with(lambda r: httpx.Response(status, json=body))
    with pytest.raises(PayByLinkError) as info:
        client.create_link(licence_request())
    assert body["errors"][0] in str(info.value)


def test_empty_error_list_gets_actionable_message():
    # Real demo response observed for portal oxfordtesting (JSON Links not enabled).
    client, _ = client_with(lambda r: httpx.Response(
        409, json={"errors": [], "url": "https://payment.demo.flywire.com?destination=D0226"}))
    with pytest.raises(PayByLinkError, match="JSON Links are enabled"):
        client.create_link(licence_request())


def test_wrong_secret_is_authentication_error():
    client, _ = client_with(lambda r: httpx.Response(401))
    with pytest.raises(AuthenticationError, match="shared secret"):
        client.create_link(licence_request())


@pytest.mark.parametrize("url", [
    "https://evil.example.com/orders/x?token=y",
    "http://payment.demo.flywire.com/orders/x?token=y",
    "https://payment.demo.flywire.com.evil.com/orders/x?token=y",
    "https://payment.demo.flywire.com/orders/x",          # no token
    "https://gateway.demo.flywire.com/redirect/not-a-uuid",
    "https://gateway.demo.flywire.com/redirect/0e71e21d-b74a-44d4-be39-6409fb34dbac?next=https://evil.example.com",
    "https://evil.example.com/redirect/0e71e21d-b74a-44d4-be39-6409fb34dbac",
    "https://gateway.demo.flywire.com.evil.com/redirect/0e71e21d-b74a-44d4-be39-6409fb34dbac",
    "https://www.demo.flywire.com/select-school",          # the generic page from error bodies
    None,
])
def test_unexpected_link_rejected(url):
    client, _ = client_with(lambda r: httpx.Response(200, json={"url": url}))
    with pytest.raises(PayByLinkError):
        client.create_link(licence_request())


def test_network_error_and_no_redirect_following():
    def boom(request):
        raise httpx.ConnectError("down")
    client, _ = client_with(boom)
    with pytest.raises(TransportError):
        client.create_link(licence_request())
    client, seen = client_with(lambda r: httpx.Response(302, headers={"Location": "https://evil.example.com"}))
    with pytest.raises(PayByLinkError):
        client.create_link(licence_request())
    assert len(seen) == 1


def test_secret_not_in_repr():
    assert SECRET not in repr(PayByLinkClient(SECRET, http_client=httpx.Client()))


# --------------------------------------------------------------------------- fulfilment


def callback(status="guaranteed", amount="12500", currency="GBP", ref="HALO-1234", **data):
    body = json.dumps({
        "event_type": status, "event_date": "2026-10-21T10:00:00Z", "event_resource": "payments",
        "data": {"payment_id": "DUN123456789", "status": status, "amount_to": amount, "currency_to": currency,
                 "external_reference": ref, **data},
    }).encode()
    return WebhookVerifier(SECRET).parse_payment_notification(body, flywire_digest(SECRET, body))


EXPECT = Expectation(callback_id="HALO-1234", amount=12500, currency="GBP")


@pytest.mark.parametrize("kwargs, decision", [
    ({}, Decision.GRANT),
    ({"status": "delivered"}, Decision.GRANT),
    ({"currency": "gbp"}, Decision.GRANT),
    ({"status": "initiated"}, Decision.WAIT),
    ({"status": "processed"}, Decision.WAIT),
    ({"status": "failed", "reason": "card declined"}, Decision.DENY),
    ({"status": "cancelled", "cancellation_reason": "expired"}, Decision.DENY),
    ({"status": "reversed"}, Decision.REVOKE),
    ({"amount": "100"}, Decision.MISMATCH),
    ({"currency": "USD"}, Decision.MISMATCH),
    ({"ref": "HALO-9999"}, Decision.MISMATCH),
    ({"ref": None}, Decision.MISMATCH),
    ({"amount": None}, Decision.MISMATCH),
    ({"status": "some_new_status"}, Decision.WAIT),
])
def test_fulfilment_decisions(kwargs, decision):
    assert FulfilmentPolicy().evaluate(callback(**kwargs), EXPECT).decision is decision


def test_callback_with_forged_digest_never_reaches_policy():
    body = b'{"event_type":"guaranteed"}'
    with pytest.raises(Exception, match="digest"):
        WebhookVerifier(SECRET).parse_payment_notification(body, flywire_digest("attacker", body))
