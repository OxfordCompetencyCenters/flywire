// Drop-in client for the Flywire Payment Gateway. No dependencies (Node 18+, uses global fetch).
//
//   import { GatewayClient, verifyWebhook } from "./flywire-gateway.mjs";
//   const gateway = new GatewayClient("https://payments.example.ac.uk", process.env.PAYMENT_GATEWAY_API_KEY);
//   const checkout = await gateway.createCheckout({ orderReference: "INV-1001", amount: 12500,
//     recipientFields: { student_email: "s@example.ac.uk", ... } });
//   res.redirect(checkout.payment_url);
//
//   // webhook route — use the RAW body (e.g. express.raw({ type: "application/json" }))
//   if (!verifyWebhook(process.env.PAYMENT_GATEWAY_WEBHOOK_SECRET, req.get("X-Gateway-Signature"), req.body)) {
//     return res.sendStatus(400);
//   }

import { createHmac, timingSafeEqual } from "node:crypto";

export const SIGNATURE_TOLERANCE_SECONDS = 300;

export class GatewayError extends Error {
  constructor(status, code, message, details) {
    super(`${status} ${code}: ${message}`);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export class GatewayClient {
  #apiKey;

  constructor(baseUrl, apiKey, { timeoutMs = 15000 } = {}) {
    const url = new URL(baseUrl);
    const local = ["localhost", "127.0.0.1"].includes(url.hostname);
    if (url.protocol !== "https:" && !(local && url.protocol === "http:")) throw new Error("baseUrl must be https://");
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.#apiKey = apiKey;
    this.timeoutMs = timeoutMs;
  }

  /** amount is in currency subunits (12500 = £125.00). Repeating orderReference returns the same checkout. */
  createCheckout({ orderReference, amount, description, payer, recipientFields, metadata, sessionType = "one_off" }) {
    const body = { order_reference: orderReference, amount, session_type: sessionType };
    if (description) body.description = description;
    if (payer) body.payer = payer;
    if (recipientFields) body.recipient_fields = recipientFields;
    if (metadata) body.metadata = metadata;
    return this.#request("POST", "/v1/checkouts", body);
  }

  getCheckout(checkoutId) {
    return this.#request("GET", `/v1/checkouts/${encodeURIComponent(checkoutId)}`);
  }

  cancelCheckout(checkoutId) {
    return this.#request("POST", `/v1/checkouts/${encodeURIComponent(checkoutId)}/cancel`, {});
  }

  async #request(method, path, body) {
    const response = await fetch(this.baseUrl + path, {
      method,
      headers: {
        Authorization: `Bearer ${this.#apiKey}`,
        Accept: "application/json",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      redirect: "error",
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = data.error || {};
      throw new GatewayError(response.status, error.code || "http_error", error.message || response.statusText, error.details);
    }
    return data;
  }
}

/** Verify `X-Gateway-Signature: t=<unix>,v1=<hex>` over the exact raw body (Buffer or string). */
export function verifyWebhook(secret, signatureHeader, rawBody, toleranceSeconds = SIGNATURE_TOLERANCE_SECONDS) {
  if (!signatureHeader || rawBody == null) return false;
  const parts = Object.fromEntries(signatureHeader.split(",").map((p) => {
    const i = p.indexOf("=");
    return [p.slice(0, i), p.slice(i + 1)];
  }));
  const timestamp = Number.parseInt(parts.t, 10);
  if (!Number.isFinite(timestamp) || !parts.v1) return false;
  if (Math.abs(Date.now() / 1000 - timestamp) > toleranceSeconds) return false;
  const body = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(String(rawBody), "utf8");
  const expected = createHmac("sha256", secret).update(`${timestamp}.`).update(body).digest("hex");
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(parts.v1, "utf8");
  return a.length === b.length && timingSafeEqual(a, b);
}
