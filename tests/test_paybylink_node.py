"""Cross-checks the Node reference (clients/node/flywire-paybylink.mjs) against the Python module."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flywire_checkout.paybylink import Decision, Expectation, FulfilmentPolicy, flywire_digest
from flywire_checkout.webhooks import WebhookVerifier

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")
MODULE = (Path(__file__).resolve().parents[1] / "clients/node/flywire-paybylink.mjs").as_uri()
SECRET = "portal-shared-secret"


def run_node(script: str) -> dict:
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_node_signs_and_builds_same_contract():
    result = run_node(f"""
import {{ buildJsonLinkBody, flywireDigest }} from "{MODULE}";
const body = buildJsonLinkBody({{ payment_destination: "oxfordtesting", amount: 12500, callback_id: "HALO-1",
  dynamic_fields: {{ student_email: "a@b.ac.uk" }}, days_to_expire: 7 }});
const bad = [];
for (const r of [{{ payment_destination: "x", callback_id: "c" }}, {{ payment_destination: "x", amount: 5, max_amount: 1, callback_id: "c" }},
                 {{ payment_destination: "x", amount: 5 }}, {{ payment_destination: "x", amount: 5, callback_id: "c", callback_url: "http://a" }},
                 {{ payment_destination: "D 0226", amount: 5, callback_id: "c" }}, {{ payment_destination: "x", amount: 5, callback_id: "c", days_to_expire: 91 }}]) {{
  try {{ buildJsonLinkBody(r); bad.push("accepted"); }} catch {{ bad.push("rejected"); }}
}}
console.log(JSON.stringify({{ body: body.toString("utf8"), digest: flywireDigest("{SECRET}", body), bad }}));
""")
    body = json.loads(result["body"])
    assert body["provider"] == "PayByJSON" and body["callback_version"] == "2" and body["days_to_expire"] == "7"
    assert result["digest"] == flywire_digest(SECRET, result["body"].encode())
    assert result["bad"] == ["rejected"] * 6


def test_node_verifies_python_signed_callback_and_decides_identically():
    cases = [
        ({}, "grant"), ({"status": "delivered"}, "grant"), ({"status": "processed"}, "wait"),
        ({"status": "failed"}, "deny"), ({"status": "cancelled"}, "deny"), ({"status": "reversed"}, "revoke"),
        ({"amount_to": "100"}, "mismatch"), ({"currency_to": "USD"}, "mismatch"),
        ({"external_reference": "OTHER"}, "mismatch"),
    ]
    bodies = []
    for overrides, _ in cases:
        data = {"payment_id": "DUN1", "status": "guaranteed", "amount_to": "12500", "currency_to": "GBP",
                "external_reference": "HALO-1", **overrides}
        raw = json.dumps({"event_type": data["status"], "event_date": "2026-10-21T10:00:00Z", "data": data})
        bodies.append({"raw": raw, "digest": flywire_digest(SECRET, raw.encode())})
        notification = WebhookVerifier(SECRET).parse_payment_notification(raw.encode(), bodies[-1]["digest"])
        py = FulfilmentPolicy().evaluate(notification, Expectation("HALO-1", 12500, "GBP")).decision
        assert py is Decision(cases[len(bodies) - 1][1])

    result = run_node(f"""
import {{ verifyFlywireCallback, evaluatePayment }} from "{MODULE}";
const bodies = {json.dumps(bodies)};
const decisions = bodies.map(b => evaluatePayment(verifyFlywireCallback(b.raw, b.digest, "{SECRET}"), {{ callbackId: "HALO-1", amount: 12500, currency: "GBP" }}).decision);
const forged = verifyFlywireCallback(bodies[0].raw, bodies[0].digest, "wrong-secret");
const tampered = verifyFlywireCallback(bodies[0].raw.replace("12500", "99999"), bodies[0].digest, "{SECRET}");
console.log(JSON.stringify({{ decisions, forged, tampered }}));
""")
    assert result["decisions"] == [expected for _, expected in cases]
    assert result["forged"] is None and result["tampered"] is None


def test_node_create_link_handles_flywire_responses():
    result = run_node(f"""
import {{ createJsonLink }} from "{MODULE}";
const responses = [
  [200, {{ url: "https://payment.demo.flywire.com/orders/abc?token=xyz" }}],
  [409, {{ errors: [], url: "https://payment.demo.flywire.com?destination=D0226" }}],
  [409, {{ errors: ["Payment destination can't be blank"], url: "https://www.demo.flywire.com/select-school" }}],
  [200, {{ errors: ["Provider can't be blank"], url: "https://payment.demo.flywire.com?destination=TQQ" }}],
  [200, {{ url: "https://evil.example.com/orders/abc?token=xyz" }}],
  [401, null],
];
const seen = [];
const out = [];
for (const [status, body] of responses) {{
  globalThis.fetch = async (url, init) => {{ seen.push({{ url, digest: init.headers["X-Flywire-Digest"] }});
    return new Response(body ? JSON.stringify(body) : "", {{ status }}); }};
  try {{ const r = await createJsonLink({{ sharedSecret: "{SECRET}", request: {{ payment_destination: "oxfordtesting", amount: 12500, callback_id: "HALO-1" }} }}); out.push("link:" + r.url); }}
  catch (e) {{ out.push("error:" + e.status); }}
}}
console.log(JSON.stringify({{ out, endpoint: seen[0].url, hasDigest: !!seen[0].digest }}));
""")
    assert result["out"] == ["link:https://payment.demo.flywire.com/orders/abc?token=xyz",
                             "error:409", "error:409", "error:200", "error:200", "error:401"]
    assert result["endpoint"] == "https://gateway.demo.flywire.com/v1/transfers.json" and result["hasDigest"]
