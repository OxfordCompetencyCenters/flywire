# Pay-By-Link (JSON Links): reference for platforms like Halo

Use this when a platform (e.g. Halo ITSM) wants to take payments **without hosting a payment page or
the gateway**. The platform creates a single-use Flywire payment link, the payer pays on Flywire's
hosted portal, and Flywire's signed callbacks tell the platform when to grant the item (e.g. a licence).

```
Ticket approved ──► create JSON Link (server-side, signed) ──► send link to the user
User pays on Flywire's hosted PayEx portal (card)
Flywire ──► callback (X-Flywire-Digest) ──► verify ─► de-duplicate ─► decide ─► grant licence
```

Reference code (tested; identical behaviour in both languages):

| | Python | Node.js / TypeScript (no dependencies) |
|---|---|---|
| Create link, verify callback, decide | [`src/flywire_checkout/paybylink.py`](../src/flywire_checkout/paybylink.py) | [`clients/node/flywire-paybylink.mjs`](../clients/node/flywire-paybylink.mjs) |
| Full ticket → link → callback → licence example | [`examples/paybylink_licence.py`](../examples/paybylink_licence.py) | see the usage block at the top of the `.mjs` |

Flywire docs: [Pay-By-Link](https://developers.flywire.com/education/Content/pay-by-link-landing-page.htm) ·
[JSON Links](https://developers.flywire.com/education/Content/pay-by-link-json-links.htm) ·
[Payment Status Notifications](https://developers.flywire.com/education/Content/notifications-payment-status.htm)

---

## 1. Use JSON Links, not Intelligent Links

| | Intelligent Links | **JSON Links** |
|---|---|---|
| How it's made | A URL you build: `…/pay/payment?recipient=…&amount=12500` | `POST` to Flywire, signed with the portal's shared secret |
| Can the payer change the amount? | **Yes.** It's a URL parameter; `read_only` only locks the field on the page | **No.** The link is an opaque token |
| Reusable? | Yes, forever | **Single use.** It expires after one payment, or after 1–90 days |
| Needs a secret? | No | Yes (keep it server-side) |

For licence payments use **JSON Links**. Even then, always check the paid amount in the callback (§4).

---

## 2. What to ask Flywire for (per portal, separately for sandbox and production)

- [ ] A **dedicated portal** for licence payments: **fixed amount** (payers can't edit it), **card only**.
- [ ] The portal's **subdomain**. JSON Links take the subdomain, not the recipient ID: for D0226 it is
      `oxfordtesting`, and `D0226` is rejected with *"Payment destination is not valid"*.
- [ ] The portal's **dynamic field IDs** (D0226: `student_first_name`, `student_last_name`, `student_email`).
- [ ] The portal's **shared secret**, sent through a secure channel. It signs link requests **and** verifies
      callbacks. Ask Flywire to confirm which secret applies to Pay-By-Link for this portal.
- [ ] **JSON Links enabled** for the portal (see §6).
- [ ] A **static callback URL** on the portal pointing at your endpoint, and/or a per-link `callback_url`.
      If both are set, Flywire sends callbacks to **both**.
- [ ] Whether `GET /payments/{id}` with your API key can see Pay-By-Link payments. Flywire's docs say the
      payments list only covers payments *created via the API*. Don't depend on it: §4 doesn't need it.

---

## 3. Creating a link

`POST https://gateway.demo.flywire.com/v1/transfers.json` (sandbox) or
`https://gateway.flywire.com/v1/transfers.json` (production)

```http
Content-Type: application/json
X-Flywire-Digest: base64( HMAC-SHA256( key = portal shared secret, message = exact request body bytes ) )
```

```json
{
  "provider": "PayByJSON",
  "payment_destination": "oxfordtesting",
  "amount": 12500,
  "dynamic_fields": {"student_first_name": "Ada", "student_last_name": "Lovelace", "student_email": "ada@ox.ac.uk"},
  "sender_email": "ada@ox.ac.uk",
  "days_to_expire": "7",
  "callback_id": "HALO-1234",
  "callback_version": "2",
  "return_cta": "https://oxford.haloitsm.com/portal",
  "return_cta_name": "Back to the IT portal"
}
```

Rules (all enforced by the reference code):

- **`amount` comes from your configuration**, never from the user. Amounts are in pence (`12500` = £125.00).
- **Don't send `max_amount` for fixed prices.** It lets the payer pay *less*.
- **Always send `callback_id`** (your ticket ID) with `callback_version: "2"`. Without them, callbacks
  can't be matched to the ticket, or aren't sent at all.
- **Sign the exact bytes you send.** Serialise once, sign those bytes, send those bytes.
- **Success is HTTP 200 with `{"url": "https://payment…flywire.com/orders/…?token=…"}`.**
  **Flywire's error responses also contain a `url`** (a generic portal page), so only accept a link from a
  200 response with no `errors`, from an `https://…flywire.com` host, containing `token=`.

| Response | Meaning |
|---|---|
| 200 `{"url": …}` | Link created. It's single-use and expires after `days_to_expire` |
| 400 `{"errors": ["Malformed JSON"], "url": …}` | Body isn't valid JSON |
| 401 (empty) | `X-Flywire-Digest` wrong: wrong secret, or signed different bytes than sent |
| 409 `{"errors": [...], "url": …}` | Missing or invalid parameters (e.g. destination) |

---

## 4. Handling callbacks (the source of truth)

Flywire POSTs JSON to your callback URL with `X-Flywire-Digest`. For each callback:

1. **Verify.** Compute `base64(HMAC-SHA256(shared secret, raw body))` and compare in constant time with
   `X-Flywire-Digest`. Use the **raw bytes**, before any JSON parsing. If it doesn't match, respond `401`
   and stop.
2. **Match.** `data.external_reference` is your `callback_id`. Look up the ticket; if it's unknown,
   respond `200` and ignore it.
3. **De-duplicate.** Flywire retries a callback up to 5 more times until it gets a 2xx. Skip it if you've
   already seen `payment_id + event_type + event_date`.
4. **Order.** Ignore an event older than the last one you applied (`event_date`). Don't rely on a fixed
   status order: Flywire can re-initiate a cancelled payment.
5. **Decide:**

| Callback status | Decision |
|---|---|
| `guaranteed` / `delivered`, and `amount_to` = expected amount, and `currency_to` = expected currency | **Grant** the licence (once) |
| `guaranteed` / `delivered` but amount, currency or reference differ | **Don't grant.** Flag for a human |
| `initiated`, `processed` (and anything unknown) | Wait |
| `failed`, `cancelled` | Don't grant (a cancelled payment can later be re-initiated) |
| `reversed` | Revoke the licence (refund or unpaid) |

6. **Respond `2xx` within a few seconds.** Return a non-2xx only if *your* processing failed, so Flywire
   retries.

This needs no Payments API lookup: the callback is signed by Flywire and contains the amount, currency,
status and your reference.

---

## 5. Testing

- **Sandbox portal pages:** `https://payment.demo.flywire.com/?destination=D0226` loads (verified).
- **Card outcomes** (cardholder name on Flywire's card page): `MAGICVALUE APPROVED` succeeds,
  `MAGICVALUE AUTH` succeeds with 3DS (password `password`), and `MAGICVALUE EXPIRED`/`FRAUD`/`NOT ENOUGH
  BALANCE` fail. Use test card `4242 4201 0000 0017`, `03/30`, `737` (GBP).
- **Status flows** (payer first name): `SANDBOX_TO_DELIVERED_STATUS`, `SANDBOX_TO_CANCEL_STATUS`,
  `SANDBOX_CANCELLED_BACK_TO_PROCESS_STATUS`.
- **Repository tests:** `pytest tests/test_paybylink*.py`. These include a Node-vs-Python cross-check.

---

## 6. Current sandbox status (checked 21 Oct 2026, 13:34 UTC)

`POST https://gateway.demo.flywire.com/v1/transfers.json` for `payment_destination: "oxfordtesting"`:

| Request | Response |
|---|---|
| Correctly signed, full body | `409 {"errors": [], "url": "https://payment.demo.flywire.com?destination=D0226"}` (cf-ray `a4e0a361cf32948a-LHR`) |
| **Wrong secret** or **no digest header** | **the same 409**, not the documented 401 |
| `payment_destination: "D0226"` | `409 {"errors": ["Payment destination is not valid"]}` |
| Minimal body (no amount) | `409 {"errors": ["Id is invalid"]}` |

**Interpretation:** the subdomain resolves to D0226, but Flywire rejects every link request before
checking the signature. **JSON Links don't appear to be enabled for this portal yet** (or need a
different secret). Flywire has to enable them before the flow can be tested end to end. The reference
code reports this case as *"rejected without a reason (check JSON Links are enabled for the portal)"*.
