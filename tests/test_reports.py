"""Payment report: capture of Flywire payment details, the export endpoint and the CLI."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from flywire_checkout import FlywireClient, FlywireConfig  # noqa: E402
from flywire_checkout.gateway import create_gateway_app  # noqa: E402
from flywire_checkout.gateway.reports import major_units, to_csv  # noqa: E402
from flywire_checkout.resources.payments import validate_payment_id  # noqa: E402

from .test_gateway import (  # noqa: E402
    KEY_A,
    KEY_B,
    REQUIRED,
    SANDBOX,
    FakeFlywire,
    FakeSites,
    SHARED,
    SITES_TOML,
    auth,
    flywire_notification,
    make_service,
    new_checkout,
    pay_path,
)

PAYMENT_REF = "DUN123456789"  # what FakeFlywire's confirm endpoint returns


def payment_record(checkout_id: str, **overrides) -> dict:
    """Shape of a real sandbox GET /payments/{id} response (23 Oct 2026), test data only."""
    record = {
        "payment_id": PAYMENT_REF, "created_at": "2026-10-23T08:29:51Z", "status": "guaranteed",
        "status_transitions": {"guaranteed_at": "2026-10-23T08:29:55Z", "delivered_at": None, "cancelled_at": None},
        "exchange_rate": 0.70652, "amount_from": 17700, "currency_from": "USD", "amount_to": 12500, "currency_to": "GBP",
        "payer": {"email": "test.payer@example.com", "country": "US", "state": "AL"},
        "recipient": {"id": "D0226", "fields": [
            {"id": "student_first_name", "value": "Ada"}, {"id": "student_last_name", "value": "Lovelace"},
            {"id": "student_email", "value": "ada@example.ac.uk"}, {"id": "statement_narrative", "value": "Uni of Oxford"}]},
        "payment_method_details": {"type": "card", "brand": "VISA", "card_classification": "CREDIT",
                                   "last_four_digits": "1111", "card_expiration": "03/2030"},
        "payouts": [{"recipient_id": "D0226", "status": "pending", "amount": 12500, "currency": "GBP"}],
        "charge_intent": {"initiator": "USER", "mode": "ONE_OFF", "payor_id": "site-a_INV-1"},
        "external_reference": checkout_id,
        "new_field_from_flywire": "ignored-safely",
    }
    record.update(overrides)
    return record


class FlywireWithPayments(FakeFlywire):
    def __init__(self) -> None:
        super().__init__()
        self.payments: dict[str, dict] = {}
        self.payments_down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/payments/v1/payments/"):
            self.requests.append(request)
            if self.payments_down:
                return httpx.Response(503)
            ref = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, json=self.payments[ref]) if ref in self.payments else httpx.Response(404)
        return super().__call__(request)


@pytest.fixture
def env(tmp_path):
    sites_file = tmp_path / "sites.toml"
    sites_file.write_text(SITES_TOML)
    return sites_file


@pytest.fixture
def setup(env):
    fakes = (FlywireWithPayments(), FakeSites())
    service = make_service(env, fakes)
    return TestClient(create_gateway_app(service)), service, fakes[0]


def pay(http, flywire, ref="INV-1", **extra):
    checkout = new_checkout(http, ref=ref, **extra).json()
    path = pay_path(checkout["payment_url"])
    sid = http.get(path + "/session").json()["session_id"]
    flywire.payments[PAYMENT_REF] = payment_record(checkout["checkout_id"])
    assert http.post(path + "/confirm", json={"confirm_url": f"{SANDBOX}checkout/sessions/{sid}/confirm"}).status_code == 200
    return checkout["checkout_id"]


def report(http, key=KEY_A, **params):
    res = http.get("/v1/reports/payments", params={"format": "json", **params}, headers=auth(key))
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- capture


def test_details_captured_on_confirmation(setup):
    http, _, flywire = setup
    checkout_id = pay(http, flywire, description="AI licence 2026")
    row = report(http)["rows"][0]
    assert row["checkout_id"] == checkout_id and row["order_reference"] == "INV-1"
    # who / how much / when / what / how
    assert (row["payer_email"], row["payer_country"]) == ("test.payer@example.com", "US")
    assert (row["amount_received"], row["currency_received"]) == ("125.00", "GBP")
    assert (row["amount_charged"], row["currency_charged"], row["exchange_rate"]) == ("177.00", "USD", 0.70652)
    assert row["amount_requested_subunits"] == 1000 and row["amount_requested"] == "10.00"
    assert row["guaranteed_at"].startswith("2026-10-23T08:29:55")
    assert row["description"] == "AI licence 2026"
    assert (row["card_brand"], row["card_type"], row["card_last4"]) == ("VISA", "CREDIT", "1111")
    assert row["field_student_first_name"] == "Ada" and row["field_student_email"] == "ada@example.ac.uk"
    assert "field_statement_narrative" not in row  # portal settings are not student data


def test_callback_adds_history_and_payer_name(setup):
    http, service, flywire = setup
    checkout_id = pay(http, flywire)
    for status, minute in (("initiated", 0), ("guaranteed", 5)):
        body = json.loads(flywire_notification(checkout_id, status, f"2026-10-23T08:3{minute}:00Z", PAYMENT_REF)[0])
        body["data"]["payer"] = {"first_name": "Test", "last_name": "Payer", "email": "test.payer@example.com"}
        raw = json.dumps(body).encode()
        digest = base64.b64encode(hmac.new(SHARED.encode(), raw, hashlib.sha256).digest()).decode()
        assert http.post("/webhooks/flywire", content=raw, headers={"X-Flywire-Digest": digest}).status_code == 200
    row = report(http)["rows"][0]
    assert (row["payer_first_name"], row["payer_last_name"]) == ("Test", "Payer")  # only via callbacks
    assert row["status_history"].startswith("initiated@2026-10-23T08:30") and "guaranteed@" in row["status_history"]


def test_flywire_lookup_failure_never_breaks_payment(setup):
    http, _, flywire = setup
    flywire.payments_down = True
    pay(http, flywire)  # confirm still returns 200
    row = report(http)["rows"][0]
    assert row["checkout_status"] == "confirmed" and row["payment_reference"] == "DUN123456789"
    assert row["payer_email"] is None
    flywire.payments_down = False
    row = report(http, refresh="true")["rows"][0]  # refresh fills the gap later
    assert row["payer_email"] == "test.payer@example.com"


# --------------------------------------------------------------------------- export


def test_csv_export(setup):
    http, _, flywire = setup
    pay(http, flywire)
    res = http.get("/v1/reports/payments", params={"from": "2000-01-01", "to": "2100-01-01"}, headers=auth())
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert 'filename="payments_site-a_2000-01-01_2100-01-01.csv"' in res.headers["content-disposition"]
    assert res.headers["cache-control"] == "no-store"
    rows = list(csv.DictReader(io.StringIO(res.text)))
    assert len(rows) == 1 and rows[0]["payer_email"] == "test.payer@example.com" and rows[0]["amount_received"] == "125.00"


def test_sites_only_see_their_own_payments(setup):
    http, _, flywire = setup
    pay(http, flywire)
    new_checkout(http, ref="B-1", key=KEY_B)
    assert [r["site_id"] for r in report(http)["rows"]] == ["site-a"]
    assert [r["site_id"] for r in report(http, key=KEY_B)["rows"]] == ["site-b"]
    assert http.get("/v1/reports/payments").status_code == 401


def test_date_and_status_filters(setup):
    http, service, flywire = setup
    pay(http, flywire)
    old = new_checkout(http, ref="OLD-1").json()["checkout_id"]
    service.store.update(old, created_at=datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc))
    today = datetime.now(timezone.utc).date()
    assert [r["order_reference"] for r in report(http, **{"from": "2026-01-15", "to": "2026-01-15"})["rows"]] == ["OLD-1"]
    assert report(http, **{"from": "2026-01-16", "to": "2026-01-16"})["count"] == 0
    assert {r["order_reference"] for r in report(http, **{"from": str(today - timedelta(days=1))})["rows"]} == {"INV-1"}
    assert [r["order_reference"] for r in report(http, status="guaranteed")["rows"]] == ["INV-1"]
    assert [r["order_reference"] for r in report(http, status="created")["rows"]] == ["OLD-1"]
    bad = http.get("/v1/reports/payments", params={"from": "2026-02-01", "to": "2026-01-01"}, headers=auth())
    assert bad.status_code == 422
    assert http.get("/v1/reports/payments", params={"from": "yesterday"}, headers=auth()).status_code == 422


def test_csv_formula_injection_neutralised():
    rows = [{"payer_email": "=HYPERLINK(\"http://evil\")", "description": "+cmd", "order_reference": "@SUM(1)",
             "card_last4": "1111", "amount_received": "125.00"}]
    text = to_csv(rows, ["payer_email", "description", "order_reference", "card_last4", "amount_received"])
    data = next(csv.DictReader(io.StringIO(text)))
    assert data["payer_email"].startswith("'=") and data["description"] == "'+cmd" and data["order_reference"] == "'@SUM(1)"
    assert data["card_last4"] == "1111" and data["amount_received"] == "125.00"


@pytest.mark.parametrize("amount, currency, expected", [
    (12500, "GBP", "125.00"), (5000, "JPY", "5000"), (12345, "BHD", "12.345"), (None, "GBP", None), (100, None, None),
])
def test_major_units(amount, currency, expected):
    assert major_units(amount, currency) == expected


@pytest.mark.parametrize("bad", ["../x", "ab", "16QF1E6TF0D0226?x=1", "a" * 41, ""])  # real ref shape + attacks
def test_payment_reference_validated(bad):
    with pytest.raises(ValueError):
        validate_payment_id(bad)


def test_payments_resource_parses_real_shape():
    def handler(request):
        assert request.url.path == f"/payments/v1/payments/{PAYMENT_REF}"
        return httpx.Response(200, json=payment_record("chk_x"))
    client = FlywireClient(FlywireConfig(api_key="k"), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    payment = client.payments.get(PAYMENT_REF)
    assert payment.status_transitions.guaranteed_at.isoformat() == "2026-10-23T08:29:55+00:00"
    assert payment.recipient_field_values()["student_email"] == "ada@example.ac.uk"


# --------------------------------------------------------------------------- CLI


def test_cli_report_writes_private_file(tmp_path, env, monkeypatch, capsys):
    from flywire_checkout.gateway import __main__ as cli
    fakes = (FlywireWithPayments(), FakeSites())
    service = make_service(env, fakes)
    http = TestClient(create_gateway_app(service))
    pay(http, fakes[0])
    monkeypatch.setattr("flywire_checkout.gateway.build_gateway_service", lambda: service)
    out = tmp_path / "payments.csv"
    out.write_text("old")
    os.chmod(out, 0o644)  # pre-existing, world-readable
    assert cli.main(["report", "--site", "site-a", "--output", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["order_reference"] == "INV-1" and rows[0]["payer_email"] == "test.payer@example.com"
    assert cli.main(["report", "--site", "nope"]) == 2
    assert cli.main(["report", "--from", "2026-02-01", "--to", "2026-01-01"]) == 2
    assert cli.main(["report", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_existing_database_is_upgraded(tmp_path):
    from flywire_checkout.gateway.store import SqliteGatewayStore
    db = tmp_path / "old.db"
    old = SqliteGatewayStore(db)  # then strip what this release added, to mimic the previous version
    old._db.executescript("DROP TABLE payment_details; DROP TABLE payment_events; DROP INDEX checkouts_site_created;")
    old.close()
    store = SqliteGatewayStore(db)
    tables = {r[0] for r in store._db.execute("select name from sqlite_master where type='table'")}
    assert {"checkouts", "processed_events", "payment_details", "payment_events"} <= tables
    assert store.list_checkouts(None, None, None, 10) == []
    assert REQUIRED  # shared fixture data stays importable
