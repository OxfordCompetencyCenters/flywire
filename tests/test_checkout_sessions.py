import httpx
import pytest

from flywire_checkout import (
    AuthenticationError,
    FlywireAPIError,
    FlywireClient,
    FlywireConfig,
    InvalidConfirmUrlError,
    RateLimitError,
    ServerError,
    SessionAlreadyCompletedError,
    TransportError,
    UnprocessableEntityError,
)
from flywire_checkout.models import CreateCheckoutSessionRequest, ExpireOutcome

from .conftest import SANDBOX_BASE, SESSION_ID

CREATE_RESPONSE = {
    "id": SESSION_ID,
    "expires_in_seconds": 1800,
    "hosted_form": {"url": f"https://elements.flywire.com/v1/form?session_id={SESSION_ID}", "method": "GET"},
    "warnings": [{"source": "email", "errors": [{"text": "must be a valid email", "error_type": "invalid_email"}]}],
}


def test_create_sends_auth_header_and_body(client, recorder):
    recorder.handler = lambda r: httpx.Response(201, json=CREATE_RESPONSE)
    session = client.checkout_sessions.create(CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=33000, recipient_id="TQQ"))

    req = recorder.requests[0]
    assert req.method == "POST"
    assert str(req.url) == SANDBOX_BASE + "checkout/sessions"
    assert req.headers["X-Authentication-Key"] == "test-api-key"
    assert req.headers["Content-Type"] == "application/json"
    assert recorder.json_body()["items"] == [{"id": "default", "amount": 33000}]
    assert session.id == SESSION_ID
    assert session.warnings[0].errors[0].error_type == "invalid_email"


def test_confirm_one_off(client, recorder):
    recorder.handler = lambda r: httpx.Response(
        200, json={"payment_reference": "RUC208967136", "charge_info": {"amount": 100000, "currency": "EUR"},
                   "payor": {"country": "US", "email": "p@example.com", "state": "NY"}},
    )
    result = client.checkout_sessions.confirm(SESSION_ID)
    assert str(recorder.requests[0].url) == f"{SANDBOX_BASE}checkout/sessions/{SESSION_ID}/confirm"
    assert result.payment_reference == "RUC208967136"
    assert result.charge_info.amount == 100000


def test_confirm_tokenization(client, recorder):
    recorder.handler = lambda r: httpx.Response(
        200, json={"payment_method": {"token": "232301485cfa4b36bc28", "type": "card"},
                   "mandate": {"id": "MCRUC20230330uAM584YC", "adjustments": [{"category": "surcharge"}]},
                   "new_future_field": 1},
    )
    result = client.checkout_sessions.confirm(SESSION_ID)
    assert result.payment_method.token == "232301485cfa4b36bc28"
    assert result.mandate.id == "MCRUC20230330uAM584YC"


def test_expire_outcomes(client, recorder):
    recorder.handler = lambda r: httpx.Response(204)
    assert client.checkout_sessions.expire(SESSION_ID) is ExpireOutcome.EXPIRED

    recorder.handler = lambda r: httpx.Response(404, json={"title": "Not Found", "status": 404})
    assert client.checkout_sessions.expire(SESSION_ID) is ExpireOutcome.NOT_FOUND

    recorder.handler = lambda r: httpx.Response(
        422, json={"title": "Unprocessable entity", "status": 422, "errors": [{"type": "unprocessable_entity",
                   "message": "Checkout session cannot expire because it has already been completed."}]},
    )
    with pytest.raises(SessionAlreadyCompletedError):
        client.checkout_sessions.expire(SESSION_ID)


def test_expire_malformed_id_is_not_reported_as_completed(client, recorder):
    # Real sandbox response for POST /checkout/sessions/not-a-uuid/expire
    recorder.handler = lambda r: httpx.Response(
        422, json={"type": "https://developers.flywire.com", "title": "Unprocessable Entity", "status": 422,
                   "detail": "Invalid parameters",
                   "errors": [{"type": "invalid_param", "source": "/", "param": "id", "message": "is in invalid format"}]},
    )
    with pytest.raises(UnprocessableEntityError) as info:
        client.checkout_sessions.expire("not-a-uuid")
    assert not isinstance(info.value, SessionAlreadyCompletedError)
    assert "id: is in invalid format" in str(info.value)


def test_sandbox_401_shape(client, recorder):
    # Real sandbox 401: note "status" is a string here.
    recorder.handler = lambda r: httpx.Response(
        401, json={"type": "about:blank", "status": "401", "title": "Unauthorized Request", "detail": "Invalid API Key"}
    )
    with pytest.raises(AuthenticationError, match="Invalid API Key"):
        client.checkout_sessions.confirm(SESSION_ID)


def test_problem_details_are_parsed(client, recorder):
    recorder.handler = lambda r: httpx.Response(
        422, json={"title": "Unprocessable entity", "status": 422, "detail": "Invalid parameters",
                   "errors": [{"param": "recipient_id", "type": "invalid_param", "message": "BACS requires GBP"}]},
    )
    with pytest.raises(UnprocessableEntityError) as info:
        client.checkout_sessions.create(CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=1, recipient_id="TQQ"))
    assert info.value.errors[0]["param"] == "recipient_id"
    assert "BACS requires GBP" in str(info.value)
    assert "test-api-key" not in str(info.value)


@pytest.mark.parametrize("status, exc", [(401, AuthenticationError), (403, AuthenticationError), (429, RateLimitError)])
def test_status_mapping(client, recorder, status, exc):
    recorder.handler = lambda r: httpx.Response(status, text="nope")
    with pytest.raises(exc):
        client.checkout_sessions.confirm(SESSION_ID)


def test_posts_are_never_retried(recorder):
    cfg = FlywireConfig(api_key="k", max_retries=3)
    flywire = FlywireClient(cfg, http_client=httpx.Client(transport=httpx.MockTransport(recorder)))
    recorder.handler = lambda r: httpx.Response(503)
    with pytest.raises(ServerError):
        flywire.checkout_sessions.confirm(SESSION_ID)
    assert len(recorder.requests) == 1  # confirm is single-use; a blind retry could double-act


def test_network_errors_wrapped(client, recorder):
    def boom(request):
        raise httpx.ConnectError("dns failure")
    recorder.handler = boom
    with pytest.raises(TransportError):
        client.checkout_sessions.confirm(SESSION_ID)


def test_redirects_not_followed(client, recorder):
    recorder.handler = lambda r: httpx.Response(302, headers={"Location": "https://evil.example.com/"})
    with pytest.raises(FlywireAPIError):
        client.checkout_sessions.confirm(SESSION_ID)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("bad_id", ["../recipients", "a/b", "", "x" * 200, "id?x=1", "%2e%2e"])
def test_session_id_path_injection_blocked(client, recorder, bad_id):
    with pytest.raises(ValueError):
        client.checkout_sessions.confirm(bad_id)
    assert recorder.requests == []


def test_confirm_url_accepts_documented_shape(client):
    url = f"{SANDBOX_BASE}checkout/sessions/{SESSION_ID}/confirm"
    assert client.checkout_sessions.session_id_from_confirm_url(url) == SESSION_ID


@pytest.mark.parametrize(
    "url",
    [
        f"https://evil.example.com/payments/v1/checkout/sessions/{SESSION_ID}/confirm",
        f"https://api-platform-sandbox.flywire.com.evil.com/payments/v1/checkout/sessions/{SESSION_ID}/confirm",
        f"http://api-platform-sandbox.flywire.com/payments/v1/checkout/sessions/{SESSION_ID}/confirm",
        f"https://user:pw@api-platform-sandbox.flywire.com/payments/v1/checkout/sessions/{SESSION_ID}/confirm",
        f"https://api-platform-sandbox.flywire.com:8443/payments/v1/checkout/sessions/{SESSION_ID}/confirm",
        f"https://api-platform-sandbox.flywire.com/payments/v1/checkout/sessions/{SESSION_ID}/confirm?x=1",
        f"https://api-platform-sandbox.flywire.com/payments/v1/checkout/sessions/{SESSION_ID}/expire",
        "https://api-platform-sandbox.flywire.com/payments/v1/checkout/sessions/../../recipients/confirm",
        # production URL while configured for sandbox — environments must never mix
        f"https://api-platform.flywire.com/payments/v1/checkout/sessions/{SESSION_ID}/confirm",
        "javascript:alert(1)",
    ],
)
def test_confirm_url_rejects_untrusted(client, url):
    with pytest.raises(InvalidConfirmUrlError):
        client.checkout_sessions.session_id_from_confirm_url(url)
