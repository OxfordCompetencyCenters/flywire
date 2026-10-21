// Flywire Pay-By-Link (JSON Links) reference for Node 18+ — no dependencies. Works from TypeScript
// (JSDoc types below) and ports line-for-line to any language.
//
//   import { createJsonLink, verifyFlywireCallback, evaluatePayment } from "./flywire-paybylink.mjs";
//
//   // 1) When a licence request is approved: create a single-use payment link.
//   const { url } = await createJsonLink({
//     environment: "sandbox",
//     sharedSecret: process.env.FLYWIRE_PORTAL_SHARED_SECRET,
//     request: {
//       payment_destination: "oxfordtesting",           // portal SUBDOMAIN, not the recipient id
//       amount: 12500,                                  // £125.00 in pence — from your config, never the user
//       callback_id: "HALO-1234",                       // your ticket id; comes back as external_reference
//       dynamic_fields: { student_first_name: "Ada", student_last_name: "Lovelace", student_email: "ada@ox.ac.uk" },
//       days_to_expire: 7,
//     },
//   });
//
//   // 2) Callback endpoint (raw body!):
//   const event = verifyFlywireCallback(rawBody, req.headers["x-flywire-digest"], process.env.FLYWIRE_PORTAL_SHARED_SECRET);
//   if (!event) return res.status(401).end();
//   const { decision } = evaluatePayment(event, { callbackId: ticket.id, amount: 12500, currency: "GBP" });
//   if (decision === "grant") addUserToLicenceGroup(ticket);

import { createHmac, timingSafeEqual } from "node:crypto";

export const JSON_LINK_ENDPOINTS = {
  sandbox: "https://gateway.demo.flywire.com/v1/transfers.json",
  production: "https://gateway.flywire.com/v1/transfers.json",
};

/** base64(HMAC-SHA256(secret, bytes)) — used both to sign link requests and to verify callbacks. */
export function flywireDigest(sharedSecret, body) {
  return createHmac("sha256", sharedSecret).update(body).digest("base64");
}

export class PayByLinkError extends Error {
  constructor(status, message, errors = []) {
    super(`HTTP ${status}: ${message}${errors.length ? " — " + errors.join("; ") : ""}`);
    this.status = status;
    this.errors = errors;
  }
}

const isHttps = (u) => { try { return new URL(u).protocol === "https:"; } catch { return false; } };

/**
 * @typedef {Object} JsonLinkRequest
 * @property {string} payment_destination  Portal subdomain.
 * @property {number} [amount]             Subunits. Required unless the portal uses items.
 * @property {Record<string, number>} [items]
 * @property {number} [max_amount]         Lets the payer pay LESS — leave unset for fixed prices.
 * @property {string} callback_id          Your reference (ticket id).
 * @property {Record<string, string>} [dynamic_fields]
 * @property {number} [days_to_expire]    1–90.
 * @property {string} [callback_url]       https; with a static portal URL configured, callbacks go to both.
 * @property {string} [return_cta]         https URL for the "return" button.
 * @property {string} [return_cta_name]
 * @property {string} [locale]
 * @property {string} [country] @property {string} [sender_email] @property {string} [sender_first_name]
 * @property {string} [sender_last_name] @property {boolean} [allow_to_edit_payer_information]
 */

/** Validate and build the exact body to sign and send. */
export function buildJsonLinkBody(request) {
  const r = { ...request };
  if ((r.amount == null) === (r.items == null)) throw new Error("provide exactly one of amount or items");
  if (r.amount != null && !(Number.isInteger(r.amount) && r.amount > 0)) throw new Error("amount must be a positive integer (subunits)");
  const total = r.amount ?? Object.values(r.items).reduce((a, b) => a + b, 0);
  if (r.max_amount != null && total > r.max_amount) throw new Error("amount must not exceed max_amount");
  if (!r.callback_id) throw new Error("callback_id is required to match callbacks to the request");
  if (!/^[A-Za-z0-9-]{1,63}$/.test(r.payment_destination ?? "")) throw new Error("payment_destination must be the portal subdomain");
  for (const k of ["callback_url", "return_cta"]) if (r[k] != null && !isHttps(r[k])) throw new Error(`${k} must be https`);
  if (r.days_to_expire != null) {
    if (!(Number.isInteger(r.days_to_expire) && r.days_to_expire >= 1 && r.days_to_expire <= 90)) throw new Error("days_to_expire must be 1–90");
    r.days_to_expire = String(r.days_to_expire);
  }
  const body = { provider: "PayByJSON", ...r, callback_version: "2" };
  for (const k of Object.keys(body)) if (body[k] == null) delete body[k];
  return Buffer.from(JSON.stringify(body), "utf8");
}

/** Create a single-use JSON Link. Resolves to { url, callbackId }. */
export async function createJsonLink({ environment = "sandbox", sharedSecret, request, timeoutMs = 30000 }) {
  if (!sharedSecret) throw new Error("sharedSecret is required");
  const endpoint = JSON_LINK_ENDPOINTS[environment];
  if (!endpoint) throw new Error(`unknown environment ${environment}`);
  const body = buildJsonLinkBody(request);
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-Flywire-Digest": flywireDigest(sharedSecret, body) },
    body,
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
  });
  const data = await response.json().catch(() => null);
  if (response.status === 401) throw new PayByLinkError(401, "X-Flywire-Digest rejected (wrong shared secret for this portal)");
  const errors = Array.isArray(data?.errors) ? data.errors.map(String) : [];
  // Flywire error bodies also contain a "url" (a generic portal page) — only trust a clean 200.
  if (response.status !== 200 || errors.length) {
    throw new PayByLinkError(response.status, errors.length ? "Pay-By-Link request rejected"
      : "Pay-By-Link request rejected without a reason (check JSON Links are enabled for the portal)", errors);
  }
  const url = data?.url;
  let ok = false;
  try {
    const u = new URL(url);
    ok = u.protocol === "https:" && (u.hostname === "flywire.com" || u.hostname.endsWith(".flywire.com")) && u.searchParams.has("token");
  } catch { /* invalid */ }
  if (!ok) throw new PayByLinkError(response.status, "Unexpected Pay-By-Link response");
  return { url, callbackId: request.callback_id };
}

/**
 * Verify a Flywire callback over the RAW request body. Returns the parsed notification, or null.
 * @param {Buffer|string} rawBody @param {string|undefined} digestHeader @param {string} sharedSecret
 */
export function verifyFlywireCallback(rawBody, digestHeader, sharedSecret) {
  if (!digestHeader || rawBody == null || !sharedSecret) return null;
  const body = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(String(rawBody), "utf8");
  if (body.length > 256 * 1024) return null;
  const expected = Buffer.from(flywireDigest(sharedSecret, body), "utf8");
  const received = Buffer.from(String(digestHeader).trim(), "utf8");
  if (expected.length !== received.length || !timingSafeEqual(expected, received)) return null;
  try { return JSON.parse(body.toString("utf8")); } catch { return null; }
}

/**
 * Decide what to do with a VERIFIED callback for one request.
 * De-duplicate on `${data.payment_id}:${event_type}:${event_date}` and ignore events older than the last applied.
 * @returns {{decision: "grant"|"wait"|"revoke"|"deny"|"mismatch", reason: string}}
 */
export function evaluatePayment(notification, { callbackId, amount, currency }) {
  const data = notification?.data ?? {};
  const status = String(data.status || notification?.event_type || "");
  if ((data.external_reference ?? "") !== callbackId) return { decision: "mismatch", reason: "callback is for a different reference" };
  if (status === "reversed") return { decision: "revoke", reason: "payment reversed" };
  if (status === "failed" || status === "cancelled") {
    return { decision: "deny", reason: `payment ${status} (${data.cancellation_reason || data.reason || status})` };
  }
  if (status !== "guaranteed" && status !== "delivered") return { decision: "wait", reason: `payment ${status}` };
  const paid = Number.parseInt(String(data.amount_to), 10);
  if (!Number.isFinite(paid)) return { decision: "mismatch", reason: "amount missing from callback" };
  if (String(data.currency_to || "").toUpperCase() !== String(currency).toUpperCase()) {
    return { decision: "mismatch", reason: `currency ${data.currency_to} != ${currency}` };
  }
  if (paid !== amount) return { decision: "mismatch", reason: `amount ${paid} != ${amount}` };
  return { decision: "grant", reason: `payment ${status}, amount matches` };
}
