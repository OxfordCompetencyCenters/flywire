"""Exercises examples/paybylink_licence.py end to end with Flywire mocked."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from flywire_checkout.paybylink import PayByLinkClient, flywire_digest  # noqa: E402

SECRET = "portal-shared-secret"
LINK = "https://payment.demo.flywire.com/orders/abc?token=xyz"
spec = importlib.util.spec_from_file_location(
    "paybylink_licence", Path(__file__).resolve().parents[1] / "examples/paybylink_licence.py")
example = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = example  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(example)


@pytest.fixture
def setup():
    sent = []

    def flywire(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"url": LINK})

    store = example.TicketStore()
    store.tickets["HALO-1"] = example.Ticket("HALO-1", "Ada", "Lovelace", "ada@example.ac.uk")
    client = PayByLinkClient(SECRET, http_client=httpx.Client(transport=httpx.MockTransport(flywire)))
    return TestClient(example.create_app(client, SECRET, "oxfordtesting", store)), store, sent


def deliver(http, status, minute, amount="12500", ref="HALO-1", secret=SECRET):
    raw = json.dumps({"event_type": status, "event_date": f"2026-10-21T10:{minute:02d}:00Z", "event_resource": "payments",
                      "data": {"payment_id": "DUN1", "status": status, "amount_to": amount, "currency_to": "GBP",
                               "external_reference": ref}}).encode()
    return http.post("/flywire/callback", content=raw, headers={"X-Flywire-Digest": flywire_digest(secret, raw)})


def test_link_then_callbacks_grant_once(setup, caplog):
    http, store, sent = setup
    assert http.post("/tickets/HALO-1/payment-link").json() == {"payment_link": LINK}
    http.post("/tickets/HALO-1/payment-link")
    assert len(sent) == 1  # one link per ticket
    assert sent[0]["amount"] == 12500 and sent[0]["callback_id"] == "HALO-1"
    assert sent[0]["dynamic_fields"]["student_email"] == "ada@example.ac.uk"

    caplog.set_level("INFO", logger="licence")
    for status, minute in (("initiated", 0), ("processed", 1), ("guaranteed", 2)):
        assert deliver(http, status, minute).status_code == 200
    assert deliver(http, "guaranteed", 2).status_code == 200  # Flywire retry
    assert deliver(http, "processed", 1).status_code == 200  # stale
    ticket = store.tickets["HALO-1"]
    assert ticket.licence_granted and ticket.payment_status == "guaranteed"
    assert sum("GRANT licence" in r.message for r in caplog.records) == 1
    assert http.post("/tickets/HALO-1/payment-link").status_code == 409


def test_forged_wrong_amount_and_reversal(setup):
    http, store, _ = setup
    assert deliver(http, "guaranteed", 0, secret="attacker").status_code == 401
    assert not store.tickets["HALO-1"].licence_granted
    deliver(http, "guaranteed", 1, amount="100")
    ticket = store.tickets["HALO-1"]
    assert not ticket.licence_granted and "amount 100" in ticket.needs_review
    deliver(http, "guaranteed", 2)
    assert ticket.licence_granted
    deliver(http, "reversed", 3)
    assert not ticket.licence_granted
    assert deliver(http, "guaranteed", 4, ref="HALO-404").status_code == 200  # unknown ticket: ack, ignore
