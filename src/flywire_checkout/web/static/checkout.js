// Browser side of "Checkout with API".
// Two rendering modes, chosen automatically:
//   1. Flywire JS SDK (when a frontend key is configured) — recommended.
//   2. Hosted-form iframe + postMessage (fallback).
// The browser never sees the API key, never chooses the amount, and only forwards
// the confirm_url to our backend, which validates it before confirming.

const config = JSON.parse(document.body.dataset.flywireConfig);
const statusEl = document.getElementById("status");
const container = document.getElementById("flywire-container");
const CONTAINER_ID = "flywire-container";

document.getElementById("env").textContent = config.environment;
document.getElementById("env").dataset.env = config.environment;

function setStatus(message) {
  statusEl.textContent = message; // textContent only — never innerHTML
}

async function postJson(path, body) {
  const response = await fetch(config.apiPrefix + path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

// Failed Checkout payments send no postMessage/event at all (per Flywire's testing guide),
// so we stop waiting when the session expires instead of leaving the payer stuck.
let expiryTimer = null;
function watchExpiry(session) {
  clearTimeout(expiryTimer);
  const seconds = Number(session.expires_in_seconds) || 1800;
  expiryTimer = setTimeout(() => {
    container.replaceChildren();
    setStatus("This checkout session has expired. Please start again.");
    payButton.disabled = false;
  }, seconds * 1000);
}

async function confirm(confirmUrl) {
  clearTimeout(expiryTimer);
  setStatus("Confirming…");
  const result = await postJson("/checkout-sessions/confirm", { confirm_url: confirmUrl });
  // Confirmation is not payment success — final status arrives via webhooks.
  setStatus(`Thank you! Reference: ${result.payment_reference ?? "pending"}`);
}

async function reportError(sessionId, error) {
  try {
    await postJson(`/checkout-sessions/${encodeURIComponent(sessionId)}/client-errors`, {
      error: typeof error === "string" ? error : JSON.parse(JSON.stringify(error ?? null)),
    });
  } catch (_) { /* best effort */ }
}

async function mountWithSdk(session) {
  // FlywireSDK(), elements() and create() are all async (verified against the sandbox SDK).
  // create() validates the session with Flywire and rejects e.g. "session not found".
  const sdk = await window.FlywireSDK(config.frontendKey);
  const elements = await sdk.elements();
  const checkout = await elements.create("payment", { sessionId: session.session_id, displayMode: "container" });
  checkout.onEvent("success", (result) => {
    const url = result?.confirm_url?.url;
    if (!url) return setStatus("Unexpected response from payment form.");
    confirm(url).catch((e) => setStatus(e.message));
  });
  checkout.onEvent("error", (error) => {
    clearTimeout(expiryTimer);
    reportError(session.session_id, error);
    setStatus("Payment was not completed.");
  });
  await checkout.mount(CONTAINER_ID);
}

function mountWithIframe(session) {
  const formUrl = new URL(session.hosted_form_url);
  if (formUrl.protocol !== "https:" || !formUrl.hostname.endsWith(".flywire.com")) {
    throw new Error("Unexpected hosted form URL");
  }
  const iframe = document.createElement("iframe");
  iframe.className = "flywire-form";
  iframe.src = formUrl.href;
  iframe.title = "Flywire payment form";
  container.replaceChildren(iframe);

  const onMessage = (event) => {
    // Exact-origin + exact-window check. (The doc sample's indexOf() check is unsafe.)
    if (event.origin !== formUrl.origin || event.source !== iframe.contentWindow) return;
    const data = event.data;
    if (!data || data.source !== "checkout_session") return;
    window.removeEventListener("message", onMessage);
    if (data.success && data.confirm_url?.url) {
      confirm(data.confirm_url.url).catch((e) => setStatus(e.message));
    } else {
      clearTimeout(expiryTimer);
      reportError(session.session_id, data);
      setStatus("Payment was not completed.");
    }
  };
  window.addEventListener("message", onMessage);
}

const payButton = document.getElementById("pay");
payButton.addEventListener("click", async () => {
  payButton.disabled = true;
  try {
    setStatus("Starting checkout…");
    const orderId = document.getElementById("order-id").value.trim();
    const session = await postJson("/checkout-sessions", { order_id: orderId });
    setStatus("");
    watchExpiry(session);
    if (config.frontendKey && typeof window.FlywireSDK === "function") {
      await mountWithSdk(session);
    } else {
      mountWithIframe(session);
    }
  } catch (e) {
    setStatus(e.message);
    payButton.disabled = false;
  }
});
