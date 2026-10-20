// Minimal example website (Node 18+, no dependencies) integrating the payment gateway.
//
//   PAYMENT_GATEWAY_URL=http://localhost:8080 \
//   PAYMENT_GATEWAY_API_KEY=... PAYMENT_GATEWAY_WEBHOOK_SECRET=... \
//   node examples/node-site/server.mjs            # http://localhost:8001
//
// Shows both integration styles: redirect (/pay/redirect) and embedded iframe (/basket).

import http from "node:http";
import { GatewayClient, GatewayError, verifyWebhook } from "../../clients/node/flywire-gateway.mjs";

const GATEWAY = process.env.PAYMENT_GATEWAY_URL ?? "http://localhost:8080";
const gateway = new GatewayClient(GATEWAY, process.env.PAYMENT_GATEWAY_API_KEY);
const WEBHOOK_SECRET = process.env.PAYMENT_GATEWAY_WEBHOOK_SECRET;
const PORT = Number(process.env.PORT ?? 8001);

const seenEvents = new Set();      // de-duplicate by event id (use your database in real life)
const orders = new Map();          // order_reference -> latest known state

// Your platform decides the amount and the student details — never the browser.
function orderFor(orderReference) {
  return {
    orderReference,
    amount: 12500, // £125.00 in pence
    description: `Course fee ${orderReference}`,
    recipientFields: { student_first_name: "Ada", student_last_name: "Lovelace", student_email: "ada@example.ac.uk" },
    payer: { country: "GB" },
  };
}

async function readBody(req, limit = 256 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("body too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function send(res, status, body, type = "text/html; charset=utf-8") {
  res.writeHead(status, { "Content-Type": type, "X-Content-Type-Options": "nosniff" });
  res.end(body);
}

const escape = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    // 1) Redirect style: create the checkout server-side, send the payer to the gateway.
    if (req.method === "GET" && url.pathname === "/pay/redirect") {
      const ref = url.searchParams.get("order") ?? "ORDER-1";
      const checkout = await gateway.createCheckout(orderFor(ref));
      orders.set(ref, checkout);
      res.writeHead(303, { Location: checkout.payment_url });
      return res.end();
    }

    // 2) Embedded style: create the checkout, render the payment page inside this site.
    if (req.method === "GET" && url.pathname === "/basket") {
      const ref = url.searchParams.get("order") ?? "ORDER-2";
      const checkout = await gateway.createCheckout(orderFor(ref));
      orders.set(ref, checkout);
      return send(res, 200, `<!doctype html><meta charset="utf-8"><title>Basket</title>
<h1>Pay for ${escape(ref)}</h1><div id="payment"></div><p id="msg"></p>
<script src="${escape(GATEWAY)}/static/embed.js"></script>
<script>
  FlywireGateway.mount("#payment", ${JSON.stringify(checkout.payment_url)}, {
    onSubmitted: (e) => { document.getElementById("msg").textContent = "Submitted: " + e.checkout_id; },
    onError: (e) => { document.getElementById("msg").textContent = "Payment error: " + (e.message || ""); },
    onCancel: () => { document.getElementById("msg").textContent = "Cancelled"; },
  });
</script>`);
    }

    // 3) Return URL: never trust query parameters — ask the gateway for the real status.
    if (req.method === "GET" && url.pathname === "/payment/return") {
      const checkout = await gateway.getCheckout(url.searchParams.get("checkout_id") ?? "");
      return send(res, 200, `<p>Order ${escape(checkout.order_reference)}: ${escape(checkout.status)}</p>`);
    }

    // 4) Signed events from the gateway.
    if (req.method === "POST" && url.pathname === "/payment/events") {
      const raw = await readBody(req);
      if (!verifyWebhook(WEBHOOK_SECRET, req.headers["x-gateway-signature"], raw)) return send(res, 400, "bad signature", "text/plain");
      const event = JSON.parse(raw.toString("utf8"));
      if (!seenEvents.has(event.id)) {
        seenEvents.add(event.id);
        orders.set(event.data.order_reference, event.data);
        console.log(`[site] event ${event.type} ${event.data.order_reference} status=${event.data.status} payment=${event.data.payment_status}`);
        // Fulfil only on payment.status_changed with payment_status "guaranteed" (or your policy).
      }
      return send(res, 200, "ok", "text/plain");
    }

    if (req.method === "GET" && url.pathname === "/orders") {
      return send(res, 200, JSON.stringify(Object.fromEntries(orders)), "application/json");
    }
    send(res, 404, "not found", "text/plain");
  } catch (err) {
    const status = err instanceof GatewayError ? 502 : 500;
    console.error("[site] error:", err.message);
    send(res, status, `Payment error: ${escape(err.message)}`);
  }
});

server.listen(PORT, "127.0.0.1", () => console.log(`[site] example website on http://localhost:${PORT}`));
