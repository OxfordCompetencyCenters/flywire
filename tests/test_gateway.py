"""Multi-site gateway tests. Flywire and the receiving websites are mocked with httpx.MockTransport."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from flywire_checkout import ConfigurationError, FlywireClient, FlywireConfig, WebhookVerifier  # noqa: E402
from flywire_checkout.gateway import (  # noqa: E402
    CheckoutStatus,
    EventForwarder,
    GatewayService,
    GatewaySettings,
    SiteRegistry,
    SqliteGatewayStore,
    create_gateway_app,
    verify_event_signature,
)

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = "https://api-platform-sandbox.flywire.com/payments/v1/"
SHARED = "flywire-shared-secret"
KEY_A = "a" * 40
KEY_B = "b" * 40
HOOK_A = "h" * 40
GATEWAY_SECRET = "g" * 40
REQUIRED = {"student_first_name": "Ada", "student_last_name": "Lovelace", "student_email": "ada@example.ac.uk"}

SITES_TOML = """
[sites.site-a]
name = "Site A"
origins = ["https://a.example.ac.uk"]
return_url = "https://a.example.ac.uk/paid?x=1"
webhook_url = "https://a.example.ac.uk/hooks"
api_key_env = "KEY_A"
webhook_secret_env = "HOOK_A"
recipient_id = "D0226"
required_fields = ["student_first_name", "student_last_name", "student_email"]
session_types = ["one_off", "recurring"]
max_amount = 500000

[sites.site-b]
name = "Site B"
origins = ["https://b.example.ac.uk"]
return_url = "https://b.example.ac.uk/done"
api_key_env = "KEY_B"
"""
ENV = {"KEY_A": KEY_A, "KEY_B": KEY_B, "HOOK_A": HOOK_A}


# --------------------------------------------------------------------------- fakes


class FakeFlywire:
    """Stateful stand-in for the Flywire Checkout Sessions API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sessions: dict[str, dict] = {}
        self.fail_create: httpx.Response | None = None
        self.fail_confirm: httpx.Response | None = None
        self.expire_response: httpx.Response | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["X-Authentication-Key"] == "fw-api-key"
        path = request.url.path.removeprefix("/payments/v1/")
        if path == "checkout/sessions":
            if self.fail_create is not None:
                return self.fail_create
            sid = str(uuid.uuid4())
            self.sessions[sid] = json.loads(request.content)
            return httpx.Response(201, json={
                "id": sid, "expires_in_seconds": 1800, "warnings": [],
                "hosted_form": {"url": f"https://elements.demo.flywire.com/v1/form?session_id={sid}", "method": "GET"},
            })
        if path.endswith("/confirm"):
            if self.fail_confirm is not None:
                return self.fail_confirm
            return httpx.Response(200, json={"payment_reference": "DUN123456789", "charge_info": {"amount": 1000}})
        if path.endswith("/expire"):
            return self.expire_response or httpx.Response(204)
        return httpx.Response(404, json={"title": "Not Found", "status": 404})

    def calls(self, suffix: str) -> int:
        return sum(r.url.path.endswith(suffix) for r in self.requests)


class FakeSites:
    """Receives gateway → website events."""

    def __init__(self) -> None:
        self.events: list[tuple[httpx.Request, dict]] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        self.events.append((request, json.loads(request.content)))
        return httpx.Response(200)


@pytest.fixture
def env(tmp_path, monkeypatch):
    sites_file = tmp_path / "sites.toml"
    sites_file.write_text(SITES_TOML)
    return sites_file


@pytest.fixture
def fakes():
    return FakeFlywire(), FakeSites()


def make_service(sites_file, fakes, *, public_url="https://pay.example.ac.uk", shared=SHARED) -> GatewayService:
    flywire, site_hooks = fakes
    config = FlywireConfig(api_key="fw-api-key", shared_secret=shared, frontend_key="fk_test", recipient_id="DEF01",
                           max_retries=0)
    return GatewayService(
        settings=GatewaySettings(public_url=public_url, secret=GATEWAY_SECRET, sites_file=str(sites_file)),
        sites=SiteRegistry.load(sites_file, ENV),
        client=FlywireClient(config, http_client=httpx.Client(transport=httpx.MockTransport(flywire))),
        store=SqliteGatewayStore(":memory:"),
        forwarder=EventForwarder(httpx.Client(transport=httpx.MockTransport(site_hooks))),
        verifier=WebhookVerifier(shared) if shared else None,
    )


@pytest.fixture
def service(env, fakes):
    return make_service(env, fakes)


@pytest.fixture
def http(service):
    return TestClient(create_gateway_app(service))


def auth(key=KEY_A):
    return {"Authorization": f"Bearer {key}"}


def new_checkout(http, ref="INV-1", amount=1000, key=KEY_A, **extra):
    body = {"order_reference": ref, "amount": amount, "recipient_fields": REQUIRED, **extra}
    return http.post("/v1/checkouts", json=body, headers=auth(key))


def pay_path(payment_url: str) -> str:
    return "/" + payment_url.split("/", 3)[3]


def flywire_notification(checkout_id, status="guaranteed", date="2026-10-20T10:00:00Z", payment_id="DUN123456789"):
    body = json.dumps({
        "event_type": status, "event_date": date, "event_resource": "payments",
        "data": {"payment_id": payment_id, "status": status, "external_reference": checkout_id,
                 "amount_to": "1000", "currency_to": "GBP", "payment_method": {"type": "card"}},
    }).encode()
    digest = base64.b64encode(hmac.new(SHARED.encode(), body, hashlib.sha256).digest()).decode()
    return body, {"X-Flywire-Digest": digest}


# --------------------------------------------------------------------------- configuration


def test_sites_load_and_authenticate(env):
    registry = SiteRegistry.load(env, ENV)
    assert len(registry) == 2
    assert registry.authenticate(KEY_A).id == "site-a"
    assert registry.authenticate(KEY_B).id == "site-b"
    assert registry.authenticate("x" * 40) is None and registry.authenticate(None) is None
    assert KEY_A not in repr(registry.get("site-a"))


@pytest.mark.parametrize("mutation, message", [
    (lambda s: s.replace('api_key_env = "KEY_B"', ""), "needs api_key_env"),
    (lambda s: s.replace("https://b.example.ac.uk\"]", "http://b.example.ac.uk\"]"), "https"),
    (lambda s: s.replace('origins = ["https://a.example.ac.uk"]', 'origins = ["https://a.example.ac.uk/path"]'), "scheme://host"),
    (lambda s: s.replace('webhook_secret_env = "HOOK_A"', ""), "webhook_secret_env"),
    (lambda s: s.replace('["one_off", "recurring"]', '["tokenization"]'), "unsupported session_types"),
    (lambda s: s.replace('[sites.site-b]', '[sites.Site_B]'), "invalid"),
    (lambda s: s.replace('unknown_option', '') + '\nbogus = 1\n', "invalid"),
    (lambda s: "not = [valid", "Invalid TOML"),
])
def test_invalid_sites_rejected(tmp_path, mutation, message):
    f = tmp_path / "sites.toml"
    f.write_text(mutation(SITES_TOML))
    with pytest.raises(ConfigurationError, match=message):
        SiteRegistry.load(f, ENV)


def test_short_or_shared_keys_rejected(env):
    with pytest.raises(ConfigurationError, match="at least 32"):
        SiteRegistry.load(env, {**ENV, "KEY_A": "short"})
    with pytest.raises(ConfigurationError, match="own API key"):
        SiteRegistry.load(env, {**ENV, "KEY_B": KEY_A})


def test_gateway_settings_validation():
    ok = GatewaySettings.from_env({"GATEWAY_PUBLIC_URL": "https://pay.example.ac.uk/", "GATEWAY_SECRET": GATEWAY_SECRET})
    assert ok.public_url == "https://pay.example.ac.uk" and ok.is_https
    assert not GatewaySettings.from_env({"GATEWAY_PUBLIC_URL": "http://localhost:8080", "GATEWAY_SECRET": GATEWAY_SECRET}).is_https
    for bad in ({"GATEWAY_SECRET": GATEWAY_SECRET},
                {"GATEWAY_PUBLIC_URL": "http://pay.example.ac.uk", "GATEWAY_SECRET": GATEWAY_SECRET},
                {"GATEWAY_PUBLIC_URL": "https://pay.example.ac.uk", "GATEWAY_SECRET": "short"}):
        with pytest.raises(ConfigurationError):
            GatewaySettings.from_env(bad)


def test_repo_sites_toml_is_valid():
    registry = SiteRegistry.load(ROOT / "sites.toml", {"SITE_LOCAL_DEMO_API_KEY": "k" * 40,
                                                       "SITE_LOCAL_DEMO_WEBHOOK_SECRET": "w" * 40})
    assert registry.get("local-demo").recipient_id == "D0226"


# --------------------------------------------------------------------------- site API


def test_create_checkout_and_idempotency(http, fakes):
    first = new_checkout(http)
    assert first.status_code == 201
    body = first.json()
    assert body["status"] == "created" and body["checkout_id"].startswith("chk_")
    assert body["payment_url"].startswith("https://pay.example.ac.uk/pay/" + body["checkout_id"] + "/")
    assert "flywire_session_id" not in body and "hosted_form_url" not in body

    again = new_checkout(http)
    assert again.status_code == 200 and again.json()["checkout_id"] == body["checkout_id"]
    assert again.json()["payment_url"] == body["payment_url"]
    assert fakes[0].requests == []  # Flywire is only called when the payer opens the link

    conflict = new_checkout(http, amount=2000)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize("body, field", [
    ({"order_reference": "x", "amount": 0}, "amount"),
    ({"order_reference": "has space", "amount": 1}, "order_reference"),
    ({"order_reference": "x", "amount": 1, "unexpected": True}, "unexpected"),
    ({"order_reference": "x", "amount": 1, "payer": {"country": "gb"}}, "payer.country"),
    ({"order_reference": "x", "amount": 1, "metadata": {"bad key": "v"}}, "metadata"),
])
def test_create_validation_errors(http, body, field):
    res = http.post("/v1/checkouts", json=body, headers=auth())
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_request"
    assert any(field in d["field"] for d in res.json()["error"]["details"])


def test_site_rules_enforced(http):
    assert "student_email" in http.post("/v1/checkouts", json={"order_reference": "x", "amount": 1},
                                        headers=auth()).json()["error"]["message"]
    assert new_checkout(http, amount=500001).status_code == 422  # max_amount
    assert new_checkout(http, key=KEY_B, session_type="recurring").status_code == 422  # not enabled for B


def test_authentication_and_site_isolation(http):
    unauth = http.post("/v1/checkouts", json={"order_reference": "x", "amount": 1})
    assert unauth.status_code == 401 and unauth.json()["error"]["code"] == "unauthorized"
    assert unauth.headers["www-authenticate"] == "Bearer"
    assert http.post("/v1/checkouts", json={}, headers={"Authorization": "Bearer " + "z" * 40}).status_code == 401
    assert http.post("/v1/checkouts", json={}, headers={"Authorization": "Basic " + KEY_A}).status_code == 401
    checkout_id = new_checkout(http).json()["checkout_id"]
    assert http.get(f"/v1/checkouts/{checkout_id}", headers=auth()).status_code == 200
    assert http.get(f"/v1/checkouts/{checkout_id}", headers=auth(KEY_B)).status_code == 404  # other site
    assert http.get("/v1/checkouts/../etc", headers=auth()).status_code in (404, 422)
    # Same order_reference at another site is a separate checkout.
    other = new_checkout(http, key=KEY_B)
    assert other.status_code == 201 and other.json()["checkout_id"] != checkout_id


# --------------------------------------------------------------------------- payer flow


def test_payment_page_and_session(http, fakes):
    flywire, _ = fakes
    checkout = new_checkout(http, payer={"first_name": "Parent", "email": "p@example.com", "country": "GB"},
                            description="Tuition 2026").json()
    path = pay_path(checkout["payment_url"])

    page = http.get(path)
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'self' https://a.example.ac.uk" in csp
    assert "sandbox.main.js" in page.text and "Site A" in page.text
    assert "/static/pay.js?v=" in page.text and "__PAY_JS_VERSION__" not in page.text
    assert "fw-api-key" not in page.text and GATEWAY_SECRET not in page.text

    session = http.get(path + "/session").json()
    assert session["status"] == "open" and session["session_id"]
    assert session["frontend_key"] == "fk_test" and session["parent_origins"] == ["https://a.example.ac.uk"]
    assert session["return_url"].startswith("https://a.example.ac.uk/paid?x=1&checkout_id=" + checkout["checkout_id"])

    sent = json.loads(flywire.requests[-1].content)
    assert sent["type"] == "one_off" and sent["recipient_id"] == "D0226"
    assert sent["external_reference"] == checkout["checkout_id"]
    assert sent["payor_id"] == "site-a_INV-1"
    assert sent["recipient"]["fields"] == [{"id": k, "value": v} for k, v in REQUIRED.items()]
    assert sent["payor"] == {"first_name": "Parent", "email": "p@example.com", "country": "GB"}
    assert sent["notifications_url"] == "https://pay.example.ac.uk/webhooks/flywire"
    assert sent["options"]["form"]["locale"] == "en"

    http.get(path + "/session")  # reload reuses the live session
    assert flywire.calls("checkout/sessions") == 1


def test_session_refreshed_when_close_to_expiry(http, service, fakes):
    checkout = new_checkout(http).json()
    path = pay_path(checkout["payment_url"])
    first = http.get(path + "/session").json()["session_id"]
    service.store.update(checkout["checkout_id"],
                         flywire_session_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2))
    second = http.get(path + "/session").json()["session_id"]
    assert first != second and fakes[0].calls("checkout/sessions") == 2


def test_bad_links_rejected(http):
    checkout = new_checkout(http).json()
    path = pay_path(checkout["payment_url"])
    tampered = path[:-2] + ("AA" if not path.endswith("AA") else "BB")
    for p in (tampered, f"/pay/{checkout['checkout_id']}/x", "/pay/chk_doesnotexistdoesnotexist/abc", "/pay/../../x/y"):
        assert http.get(p).status_code == 404
        assert http.get(p + "/session").status_code == 404


def test_no_notifications_url_for_plain_http_gateway(env, fakes):
    svc = make_service(env, fakes, public_url="http://localhost:8080")
    http = TestClient(create_gateway_app(svc))
    checkout = new_checkout(http).json()
    assert checkout["payment_url"].startswith("http://localhost:8080/pay/")
    http.get(pay_path(checkout["payment_url"]) + "/session")
    assert "notifications_url" not in json.loads(fakes[0].requests[-1].content)


def test_flywire_rejection_surfaces_as_502(http, service, fakes):
    fakes[0].fail_create = httpx.Response(422, json={"title": "Unprocessable Entity", "status": 422, "errors": [
        {"type": "invalid_param", "param": "recipient_id",
         "message": "This recipient is either non-existing or has been deactivated"}]})
    checkout = new_checkout(http).json()
    res = http.get(pay_path(checkout["payment_url"]) + "/session")
    assert res.status_code == 502 and res.json()["error"]["code"] == "payment_provider_error"
    assert "recipient" not in res.json()["error"]["message"]  # upstream detail not leaked to the payer
    stored = service.store.get(checkout["checkout_id"])
    assert stored.status is CheckoutStatus.CREATED and "recipient_id" in stored.last_error


def test_confirm_flow_and_site_event(http, fakes):
    flywire, sites = fakes
    checkout = new_checkout(http, metadata={"student": "S1"}).json()
    path = pay_path(checkout["payment_url"])
    session_id = http.get(path + "/session").json()["session_id"]

    res = http.post(path + "/confirm", json={"confirm_url": f"{SANDBOX}checkout/sessions/{session_id}/confirm"})
    assert res.status_code == 200 and res.json() == {"status": "confirmed", "payment_reference": "DUN123456789"}

    request, event = sites.events[-1]
    assert event["type"] == "checkout.submitted" and event["site_id"] == "site-a"
    assert event["data"]["checkout_id"] == checkout["checkout_id"] and event["data"]["metadata"] == {"student": "S1"}
    assert request.headers["X-Gateway-Event-Id"] == event["id"]
    assert verify_event_signature(HOOK_A, request.headers["X-Gateway-Signature"], request.content)
    assert not verify_event_signature("wrong" * 8, request.headers["X-Gateway-Signature"], request.content)

    # Second confirm: idempotent, no second Flywire call.
    again = http.post(path + "/confirm", json={"confirm_url": f"{SANDBOX}checkout/sessions/{session_id}/confirm"})
    assert again.status_code == 200 and flywire.calls("/confirm") == 1
    status = http.get(f"/v1/checkouts/{checkout['checkout_id']}", headers=auth()).json()
    assert status["status"] == "confirmed" and status["payment_reference"] == "DUN123456789"
    # Session endpoint no longer hands out the Flywire session once confirmed.
    assert http.get(path + "/session").json()["session_id"] is None


@pytest.mark.parametrize("confirm_url", [
    "https://evil.example.com/payments/v1/checkout/sessions/{sid}/confirm",
    f"{SANDBOX}checkout/sessions/{uuid.uuid4()}/confirm",  # a different (real-looking) session
    "https://api-platform.flywire.com/payments/v1/checkout/sessions/{sid}/confirm",  # production host
])
def test_confirm_rejects_foreign_urls(http, fakes, confirm_url):
    checkout = new_checkout(http).json()
    path = pay_path(checkout["payment_url"])
    sid = http.get(path + "/session").json()["session_id"]
    res = http.post(path + "/confirm", json={"confirm_url": confirm_url.format(sid=sid)})
    assert res.status_code == 422 and fakes[0].calls("/confirm") == 0


def test_confirm_failure_is_retryable(http, fakes):
    flywire, _ = fakes
    checkout = new_checkout(http).json()
    path = pay_path(checkout["payment_url"])
    sid = http.get(path + "/session").json()["session_id"]
    url = f"{SANDBOX}checkout/sessions/{sid}/confirm"
    flywire.fail_confirm = httpx.Response(503)
    assert http.post(path + "/confirm", json={"confirm_url": url}).status_code == 502
    assert http.get(f"/v1/checkouts/{checkout['checkout_id']}", headers=auth()).json()["status"] == "errored"
    flywire.fail_confirm = None
    assert http.post(path + "/confirm", json={"confirm_url": url}).json()["status"] == "confirmed"


def test_site_down_does_not_break_payer_confirmation(http, fakes):
    fakes[1].down = True
    checkout = new_checkout(http).json()
    path = pay_path(checkout["payment_url"])
    sid = http.get(path + "/session").json()["session_id"]
    res = http.post(path + "/confirm", json={"confirm_url": f"{SANDBOX}checkout/sessions/{sid}/confirm"})
    assert res.status_code == 200 and res.json()["status"] == "confirmed"


def test_client_error_recorded(http, service):
    checkout = new_checkout(http).json()
    path = pay_path(checkout["payment_url"])
    assert http.post(path + "/client-error", json={"error": {"reason": "closed"}}).status_code == 204
    assert "closed" in service.store.get(checkout["checkout_id"]).last_error


# --------------------------------------------------------------------------- cancel / expiry


def test_cancel_paths(http, fakes):
    flywire, _ = fakes
    unopened = new_checkout(http, ref="C1").json()
    assert http.post(f"/v1/checkouts/{unopened['checkout_id']}/cancel", headers=auth()).json()["status"] == "cancelled"
    assert flywire.calls("/expire") == 0
    assert http.get(pay_path(unopened["payment_url"]) + "/session").status_code == 410
    assert http.post(f"/v1/checkouts/{unopened['checkout_id']}/cancel", headers=auth()).status_code == 200  # idempotent

    opened = new_checkout(http, ref="C2").json()
    http.get(pay_path(opened["payment_url"]) + "/session")
    assert http.post(f"/v1/checkouts/{opened['checkout_id']}/cancel", headers=auth()).json()["status"] == "cancelled"
    assert flywire.calls("/expire") == 1

    submitted = new_checkout(http, ref="C3").json()
    http.get(pay_path(submitted["payment_url"]) + "/session")
    flywire.expire_response = httpx.Response(422, json={"status": 422, "errors": [
        {"type": "unprocessable_entity", "message": "already completed"}]})
    res = http.post(f"/v1/checkouts/{submitted['checkout_id']}/cancel", headers=auth())
    assert res.status_code == 409


def test_checkout_link_expiry(http, service):
    checkout = new_checkout(http).json()
    service.store.update(checkout["checkout_id"], expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert http.get(pay_path(checkout["payment_url"]) + "/session").status_code == 410
    assert http.get(f"/v1/checkouts/{checkout['checkout_id']}", headers=auth()).json()["status"] == "expired"


# --------------------------------------------------------------------------- Flywire webhooks


def _open_and_confirm(http):
    checkout = new_checkout(http).json()
    path = pay_path(checkout["payment_url"])
    sid = http.get(path + "/session").json()["session_id"]
    http.post(path + "/confirm", json={"confirm_url": f"{SANDBOX}checkout/sessions/{sid}/confirm"})
    return checkout["checkout_id"]


def test_webhook_forwarded_to_site(http, fakes):
    _, sites = fakes
    checkout_id = _open_and_confirm(http)
    body, headers = flywire_notification(checkout_id, "guaranteed")
    assert http.post("/webhooks/flywire", content=body, headers=headers).status_code == 200

    request, event = sites.events[-1]
    assert event["type"] == "payment.status_changed"
    assert event["data"]["payment_status"] == "guaranteed" and event["data"]["status"] == "confirmed"
    assert event["data"]["flywire_event"]["amount_to"] == "1000"
    assert verify_event_signature(HOOK_A, request.headers["X-Gateway-Signature"], request.content)

    # Flywire retry of the same notification: acknowledged, not re-forwarded.
    count = len(sites.events)
    assert http.post("/webhooks/flywire", content=body, headers=headers).status_code == 200
    assert len(sites.events) == count


def test_webhook_retried_while_site_is_down(http, fakes):
    _, sites = fakes
    checkout_id = _open_and_confirm(http)
    body, headers = flywire_notification(checkout_id, "processed")
    sites.down = True
    assert http.post("/webhooks/flywire", content=body, headers=headers).status_code == 503  # Flywire retries
    sites.down = False
    assert http.post("/webhooks/flywire", content=body, headers=headers).status_code == 200
    assert sites.events[-1][1]["data"]["payment_status"] == "processed"


def test_webhook_security_and_unknown_refs(http, fakes):
    body, headers = flywire_notification("chk_" + "x" * 24)
    assert http.post("/webhooks/flywire", content=body, headers={"X-Flywire-Digest": "forged"}).status_code == 401
    assert http.post("/webhooks/flywire", content=body).status_code == 401
    assert http.post("/webhooks/flywire", content=body, headers=headers).status_code == 200  # unknown ref: ack + ignore
    assert fakes[1].events == []


def test_webhook_status_follows_event_time(http, service):
    checkout_id = _open_and_confirm(http)
    for status, minute in (("initiated", 0), ("cancelled", 1), ("initiated", 2), ("processed", 3), ("delivered", 5)):
        body, headers = flywire_notification(checkout_id, status, f"2026-10-20T10:0{minute}:00Z")
        http.post("/webhooks/flywire", content=body, headers=headers)
    body, headers = flywire_notification(checkout_id, "guaranteed", "2026-10-20T10:04:00Z")  # late retry
    http.post("/webhooks/flywire", content=body, headers=headers)
    assert service.store.get(checkout_id).payment_status == "delivered"


def test_webhook_without_shared_secret(env, fakes):
    svc = make_service(env, fakes, shared=None)
    body, headers = flywire_notification("chk_" + "x" * 24)
    assert TestClient(create_gateway_app(svc)).post("/webhooks/flywire", content=body, headers=headers).status_code == 503


# --------------------------------------------------------------------------- misc


def test_static_assets_and_health(http):
    assert http.get("/static/embed.js").headers["content-type"].startswith("text/javascript")
    assert http.get("/static/pay.js").status_code == 200
    assert http.get("/static/../app.py").status_code == 404
    assert http.get("/static/pay.html").status_code == 404
    assert http.get("/healthz").json() == {"status": "ok", "environment": "sandbox", "sites": 2}


def test_store_persists_across_restarts(tmp_path, env, fakes):
    db = tmp_path / "gw.db"
    svc = make_service(env, fakes)
    svc.store = SqliteGatewayStore(db)
    checkout, _ = svc.create_checkout(svc.sites.get("site-a"),
                                      __import__("flywire_checkout.gateway", fromlist=["x"]).CreateCheckoutInput(
                                          order_reference="P1", amount=5, recipient_fields=REQUIRED))
    svc.store.close()
    reopened = SqliteGatewayStore(db).get(checkout.id)
    assert reopened.order_reference == "P1" and reopened.recipient_fields == REQUIRED


def test_python_drop_in_client_verifier_matches_gateway():
    spec = importlib.util.spec_from_file_location("fgc", ROOT / "clients/python/flywire_gateway_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from flywire_checkout.gateway.events import sign
    import time
    body = b'{"id":"evt_1"}'
    header = sign(HOOK_A, int(time.time()), body)
    assert module.verify_webhook(HOOK_A, header, body)
    assert not module.verify_webhook(HOOK_A, header, body + b" ")
    assert not module.verify_webhook(HOOK_A, sign(HOOK_A, int(time.time()) - 600, body), body)  # too old
    assert not module.verify_webhook(HOOK_A, "garbage", body)


def test_payment_link_tokens_redacted_from_access_logs(http, caplog):
    import logging
    checkout = new_checkout(http).json()
    token = checkout["payment_url"].rsplit("/", 1)[1]
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:1", "GET", pay_path(checkout["payment_url"]) + "/session", "1.1", 200), None)
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    rendered = record.getMessage()
    assert token not in rendered and checkout["checkout_id"] + "/<redacted>/session" in rendered
