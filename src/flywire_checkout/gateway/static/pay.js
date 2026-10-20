// Hosted payment page. Works standalone (redirect mode) or inside an iframe (embed mode).
// Never trusts window.parent blindly: messages go only to an origin listed for the site.

const { base, parent_origins: allowedParents, cancel_url: cancelUrl } = JSON.parse(document.body.dataset.config);
const container = document.getElementById("flywire-container");
const statusEl = document.getElementById("status");
const retryBtn = document.getElementById("retry");
const backLink = document.getElementById("back");
const CONTAINER_ID = "flywire-container";

const params = new URLSearchParams(window.location.search);
const embedded = window.parent !== window;
// Only ever talk to a parent whose origin is configured for this site (sites.toml "origins").
const requestedParent = params.get("embed_origin");
const parentOrigin = embedded && requestedParent && allowedParents.includes(requestedParent) ? requestedParent : null;
let session = null;
let refreshTimer = null;

function setStatus(text) { statusEl.textContent = text; }

function notifyParent(type, data = {}) {
  if (embedded && parentOrigin) window.parent.postMessage({ type: `flywire-gateway:${type}`, ...data }, parentOrigin);
}

async function api(path, body) {
  const response = await fetch(base + path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit",
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data?.error?.message || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function showActions({ retry = false, back = true } = {}) {
  retryBtn.hidden = !retry;
  backLink.hidden = !back;
}

function finish(result) {
  clearTimeout(refreshTimer);
  setStatus("Thank you — your payment details were submitted. You will receive confirmation once the payment is processed.");
  notifyParent("submitted", { checkout_id: result.checkout_id, payment_reference: result.payment_reference ?? null });
  if (!embedded && session) setTimeout(() => window.location.assign(session.return_url), 1500);
}

async function confirm(confirmUrl) {
  setStatus("Confirming your payment…");
  try {
    const result = await api("/confirm", { confirm_url: confirmUrl });
    finish({ checkout_id: base.split("/")[2], ...result });
  } catch (e) {
    setStatus(`We could not confirm your payment: ${e.message}`);
    notifyParent("error", { message: e.message });
    showActions({ retry: true });
  }
}

async function reportError(error) {
  try { await api("/client-error", { error: typeof error === "string" ? error : JSON.parse(JSON.stringify(error ?? null)) }); }
  catch (_) { /* best effort */ }
}

function onFormError(error) {
  clearTimeout(refreshTimer);
  reportError(error);
  setStatus("The payment was not completed.");
  notifyParent("error", { message: "payment_not_completed" });
  showActions({ retry: true });
}

async function mountSdk() {
  // FlywireSDK(), elements() and create() are all async (verified against the sandbox SDK).
  const sdk = await window.FlywireSDK(session.frontend_key);
  const elements = await sdk.elements({ locale: document.documentElement.lang || "en" });
  const element = await elements.create("payment", { sessionId: session.session_id, displayMode: "container" });
  element.onEvent("success", (result) => {
    const url = result?.confirm_url?.url;
    url ? confirm(url) : onFormError("success event without confirm_url");
  });
  element.onEvent("error", onFormError);
  await element.mount(CONTAINER_ID);
}

function mountIframe() {
  const formUrl = new URL(session.hosted_form_url);
  if (formUrl.protocol !== "https:" || !formUrl.hostname.endsWith(".flywire.com")) throw new Error("Unexpected form URL");
  const iframe = document.createElement("iframe");
  iframe.className = "flywire-form";
  iframe.title = "Flywire payment form";
  iframe.src = formUrl.href;
  container.replaceChildren(iframe);
  window.addEventListener("message", function onMessage(event) {
    // Exact origin + source window (the docs' indexOf() check accepts any origin).
    if (event.origin !== formUrl.origin || event.source !== iframe.contentWindow) return;
    const data = event.data;
    if (!data || data.source !== "checkout_session") return;
    window.removeEventListener("message", onMessage);
    data.success && data.confirm_url?.url ? confirm(data.confirm_url.url) : onFormError(data);
  });
}

function scheduleRefresh() {
  // Flywire sessions last 30 minutes; reload shortly before so the gateway issues a fresh one.
  clearTimeout(refreshTimer);
  if (!session.expires_at) return;
  const ms = new Date(session.expires_at).getTime() - Date.now() - 60_000;
  if (ms > 0) refreshTimer = setTimeout(() => window.location.reload(), ms);
}

function reportHeight() {
  if (!embedded || !parentOrigin || typeof ResizeObserver !== "function") return;
  const send = () => notifyParent("resize", { height: Math.ceil(document.documentElement.scrollHeight) });
  new ResizeObserver(send).observe(document.body);
  send();
}

async function start() {
  backLink.href = cancelUrl;
  backLink.addEventListener("click", (event) => {
    if (embedded && parentOrigin) { event.preventDefault(); notifyParent("cancel"); }
  });
  reportHeight(); // parent origin is known up-front, so size the iframe even on error paths
  try {
    session = await api("/session");
  } catch (e) {
    const gone = e.status === 410;
    setStatus(gone ? `This payment link is no longer valid (${e.message.split(" ").pop()}).`
                   : `${e.message}. Please try again in a moment.`);
    notifyParent("error", { message: e.message, status: e.status });
    showActions({ retry: !gone && e.status !== 404, back: true });
    return;
  }
  if (session.environment !== "production") {
    const env = document.getElementById("env");
    env.textContent = session.environment;
    env.hidden = false;
  }
  document.getElementById("description").textContent = session.description || "";

  if (session.status === "confirmed") {
    setStatus("This payment has already been submitted.");
    notifyParent("submitted", { checkout_id: base.split("/")[2], already: true });
    showActions({ back: true });
    return;
  }
  try {
    if (session.frontend_key && typeof window.FlywireSDK === "function") await mountSdk();
    else mountIframe();
    setStatus("");
    showActions({ back: true });
    scheduleRefresh();
  } catch (e) {
    onFormError(String(e));
  }
}

retryBtn.addEventListener("click", () => window.location.reload());
start();
