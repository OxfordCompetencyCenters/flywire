# flywire-checkout

One Flywire integration for **many websites**. Deploy the **payment gateway** once, register each
website in [`sites.toml`](sites.toml), and every site (Python, Node, PHP, WordPress, …) integrates
with a few HTTPS calls. **Start here: [docs/INTEGRATION.md](docs/INTEGRATION.md).**

```
Website 1 ─┐                       ┌──────────────┐
Website 2 ─┼── site API key ─────► │   gateway    │ ── Flywire API key ──► Flywire
Website 3 ─┘ ◄── signed events ─── │  sites.toml  │ ◄── signed callbacks ──
                                   └──────────────┘
```

| You want to… | Go to |
|---|---|
| Run the gateway | [INTEGRATION §2](docs/INTEGRATION.md#2-deploy-the-gateway-once) · `Dockerfile` · `python -m flywire_checkout.gateway serve` |
| Add a website | [INTEGRATION §3](docs/INTEGRATION.md#3-add-a-website-per-site-5-minutes): one block in `sites.toml` + two keys |
| Integrate a website | [INTEGRATION §4](docs/INTEGRATION.md#4-integrate-the-website-per-site) + a drop-in client from [`clients/`](clients) (Python, Node, PHP) |
| See a working site | [`examples/node-site/server.mjs`](examples/node-site/server.mjs) (redirect + embed) |
| See who paid, how much, when and for what | [INTEGRATION §5](docs/INTEGRATION.md#get-v1reportspayments-payment-report-who-how-much-when-what) (per site) · `python -m flywire_checkout.gateway report` (Finance, all sites) |
| Take payments with **no** gateway, via Flywire Pay-By-Link (e.g. Halo) | [docs/PAY_BY_LINK.md](docs/PAY_BY_LINK.md): JSON Links + signed callbacks, Python & Node |
| Embed the Flywire library directly in one Python app | the rest of this README |

---

The gateway is built on a typed, secure Python library for Flywire's **Checkout with API** flow (Education).
The library is built from the official docs:

- [Checkout with API](https://developers.flywire.com/education/Content/checkout-with-api.htm)
- [Checkout Sessions resource](https://developers.flywire.com/education/Content/resource_checkout-sessions.htm)
- [API basics](https://developers.flywire.com/education/Content/api-basics.htm) · [Sandbox](https://developers.flywire.com/education/Content/testing-sandbox-api-env.htm)
- [Notifications / X-Flywire-Digest](https://developers.flywire.com/education/Content/notifications-from-flywire.htm) · [Payment status](https://developers.flywire.com/education/Content/notifications-payment-status.htm)

## Flow

```
Browser                         Your backend (this package)                Flywire
  │  POST /api/flywire/checkout-sessions {order_id}                          │
  │ ───────────────────────────► resolve amount server-side                  │
  │                              POST /checkout/sessions ──────────────────► │
  │ ◄─────────── session_id ──── store (session → owner)                     │
  │  render SDK element / iframe ─────────────────────────────────────────► │
  │  payer submits → success event / postMessage {confirm_url}               │
  │  POST /api/flywire/checkout-sessions/confirm {confirm_url}               │
  │ ───────────────────────────► validate URL + owner + single-use           │
  │                              POST /checkout/sessions/{id}/confirm ─────► │
  │ ◄──── payment_reference ──── store reference / token / mandate           │
  │                              POST /webhooks/flywire  ◄── X-Flywire-Digest│
  │                              verify HMAC → dedupe → your handlers         │
```

Confirming a session is **not** payment success. Fulfil orders from the `guaranteed`
(or `processed` / `delivered`, depending on your policy) webhook.

## Switching sandbox ↔ production

One variable: `FLYWIRE_ENVIRONMENT=sandbox|production` (aliases: `test`, `prod`).
Credentials resolve as `FLYWIRE_<ENV>_<NAME>` then `FLYWIRE_<NAME>`, so both sets can
live in `.env` side by side. The API base URL and the JS SDK URL switch together and
cannot be mixed (a production `confirm_url` is rejected while in sandbox).

| | Sandbox | Production |
|---|---|---|
| API | `https://api-platform-sandbox.flywire.com/payments/v1/` | `https://api-platform.flywire.com/payments/v1/` |
| JS SDK | `…/sdk/js/v0/sandbox.main.js` | `…/sdk/js/v0/main.js` |

## Quick start

```bash
cp .env.example .env              # fill FLYWIRE_SANDBOX_* (never commit .env)
uv venv && uv pip install -e ".[web,dev]"
pytest
python examples/demo_server.py    # http://127.0.0.1:8000/checkout
```

Library use without the web layer:

```python
from flywire_checkout import FlywireClient
from flywire_checkout.models import CreateCheckoutSessionRequest, Payor

with FlywireClient.from_env() as flywire:
    session = flywire.checkout_sessions.create(
        CreateCheckoutSessionRequest.one_off(
            amount=33000,                  # subunits: 330.00
            recipient_id="TQQ",
            payor_id="student-42",         # your id for the payer — required by the API
            payor=Payor(email="payer@example.com", country="GB"),
            external_reference="order-123",
        )
    )
```

Factories: `one_off`, `recurring` (Flywire-hosted), `tokenization`, `tokenization_and_pay`,
`preauthorization`, `new_mandate`. Each one checks Flywire's rules for its session type
before any request is sent.

## Sandbox testing

From [Testing Checkout with API](https://developers.flywire.com/education/Content/testing-checkout-api.htm) and
[Testing Payment Flows](https://developers.flywire.com/education/Content/testing-payment-flows.htm).

**Before anything works:** `GET /recipients` must list your portal. An API application can only see
recipients that belong to the Flywire client account it was registered under.

**Card outcomes:** enter the magic value as the **cardholder name** on the card details page (not the payer name).

| Scenario | One-off / hosted recurring | Self-managed (tokenization) |
|---|---|---|
| Success with 3DS (simulator password: `password`) | `MAGICVALUE AUTH` | `RECURRING CARD AUTH` |
| Success without 3DS | `MAGICVALUE APPROVED` | `RECURRING CARD APPROVED` |
| Fail | `MAGICVALUE EXPIRED` / `FRAUD` / `NOT ENOUGH BALANCE` | `RECURRING CARD EXPIRED` / … |

Test cards (expiry `03/30`, CVV `737`): `4242 4201 0000 0017` (Visa, GBP, GB), `4111 1111 1111 1111` (Visa, USD),
`5454 5454 5454 5454` (Mastercard, USD), Amex `3700 0000 0000 002` (CVV `7373`).

**Direct debit:** account holder `MAGICVALUE APPROVED`. BACS sort code `200000`, account `55667788`.
SEPA IBAN `DE89370400440532013000`.

**Payment-status flows:** enter as the **payer's first name** and stop at the card/bank details screen.
Examples: `SANDBOX_TO_DELIVERED_STATUS`, `SANDBOX_TO_CANCEL_STATUS`,
`SANDBOX_CANCELLED_BACK_TO_PROCESS_STATUS` (cancelled is not always final), `SANDBOX_TO_REVERSED_STATUS`,
`SANDBOX_SMALL_UNDERPAYMENT_STATUS` (guaranteed amount differs from initiated, so fulfil on the guaranteed amount).

Things the integration handles because of these docs:
- **Failed Checkout card payments send no postMessage.** The page gives up when the session expires.
- **Webhook status follows `event_date`, not a fixed order.** A payment can legitimately go cancelled → initiated → processed.
- **`recipient.fields` goes top-level**, and each portal's required fields come from `GET /recipients/{id}`.

## Layout

| Module | Responsibility |
|---|---|
| `environments.py` | Per-environment URLs (single source of truth) |
| `config.py` | Env-driven config, `SecretStr` secrets, HTTPS enforcement |
| `http.py` | Only place the API key is used: timeouts, no redirects, safe retries, error mapping, PII-free logs |
| `models/` | Strict request models, forward-compatible response/notification models |
| `resources/checkout_sessions.py` | `create` / `confirm` / `expire` + confirm-URL validation |
| `webhooks.py` | `X-Flywire-Digest` verification (HMAC-SHA256, base64, constant-time) |
| `services/` | Ownership, single-confirm, webhook dedupe, handler dispatch; `CheckoutStore` port |
| `web/` | Optional FastAPI adapter + checkout page (SDK or iframe) for a single Python app |
| `gateway/` | Multi-site payment gateway: site registry, SQLite store, hosted payment page, signed events, CLI |
| `clients/` | Drop-in website clients: Python (stdlib), Node (no deps), PHP (curl); Node Pay-By-Link reference |
| `paybylink.py` | Flywire Pay-By-Link JSON Links (signed creation) + callback fulfilment policy |

## Security controls

- API key only in the backend, only sent to the configured Flywire host; redirects are never followed.
- **The `confirm_url` from the browser is untrusted.** It must match the configured host and path exactly. The session ID is extracted and the URL is rebuilt server-side, so a forged URL can't steal the API key (SSRF).
- **Sessions are bound to the authenticated user.** Confirm and expire check ownership, and confirm runs at most once (an atomic store claim).
- **The amount is resolved server-side from an order ID.** Request bodies reject unknown fields.
- **Webhooks:** HMAC over the raw bytes, constant-time compare, size cap, idempotent processing. A 5xx response on handler failure makes Flywire retry the delivery.
- **POSTs are never auto-retried** (confirm is single-use), and neither is a 429. Flywire blocks the IP for 10 minutes on a 429.
- **Browser:** exact-origin + source-window `postMessage` check, where the docs' sample `indexOf` check is unsafe. The page also gets a CSP, `frame-ancestors 'none'`, and only `textContent` rendering.
- **Upstream errors are logged server-side** and translated into generic messages for the client.

## Production checklist

- Implement `CheckoutStore` on your database; make `claim_for_confirmation` / `claim_event` atomic.
- Replace `owner_id_provider` with real authentication; add CSRF protection if you use cookie sessions.
- Serve behind HTTPS; set `FLYWIRE_NOTIFICATIONS_URL` to the public webhook URL.
- Keep secrets in a secrets manager, rotate on staff changes, and use different keys per environment.
- Handle `RateLimitError` (500 req/min per IP) with alerting.
