"""Live tests against the Flywire sandbox. Opt-in only:

    FLYWIRE_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_live_sandbox.py -v

Reads credentials from `.env`. Refuses to run against production. Uses a handful of
requests (well under the 500 req/min limit); every session it creates is expired again.
"""

from __future__ import annotations

import os
import uuid

import pytest

if os.environ.get("FLYWIRE_LIVE_TESTS") != "1":
    pytest.skip("live sandbox tests are opt-in (FLYWIRE_LIVE_TESTS=1)", allow_module_level=True)

from dotenv import load_dotenv  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from flywire_checkout import (  # noqa: E402
    AuthenticationError,
    FlywireClient,
    FlywireConfig,
    NotFoundError,
    SessionAlreadyCompletedError,
    UnprocessableEntityError,
)
from flywire_checkout.http import HttpTransport  # noqa: E402
from flywire_checkout.models import (  # noqa: E402
    ChargeMode,
    CreateCheckoutSessionRequest,
    ExpireOutcome,
    PaymentSchema,
    Payor,
    Recipient,
)

# Above the portal minimum (D0226: "Amount must be greater than £50.00").
LIVE_AMOUNT = 12500

load_dotenv()
CONFIG = FlywireConfig.from_env()
if CONFIG.is_production:
    pytest.exit("live tests must never run against production", returncode=2)


@pytest.fixture(scope="module")
def flywire():
    with FlywireClient(CONFIG) as client:
        yield client


@pytest.fixture(scope="module")
def recipient_details() -> dict | None:
    try:
        return HttpTransport(CONFIG).request("GET", f"recipients/{CONFIG.recipient_id}").json()
    except NotFoundError:
        return None


@pytest.fixture(scope="module")
def recipient_available(recipient_details) -> bool:
    return recipient_details is not None


def _required_fields(recipient_details: dict | None) -> Recipient | None:
    """Fill every field the portal marks as required with a test value."""
    fields = [f for f in (recipient_details or {}).get("fields", []) if f.get("required")]
    if not fields:
        return None
    return Recipient.from_values({f["id"]: "test@example.com" if "email" in f["id"] else "Test" for f in fields})


def test_api_key_is_valid():
    response = HttpTransport(CONFIG).request("GET", "recipients")
    assert "recipients" in response.json()


def test_wrong_key_rejected():
    bad = CONFIG.model_copy(update={"api_key": SecretStr("not-a-real-key")})
    with pytest.raises(AuthenticationError):
        HttpTransport(bad).request("GET", "recipients")


def test_configured_recipient_exists(recipient_available):
    assert recipient_available, (
        f"Recipient {CONFIG.recipient_id} is not visible to this API key "
        "(GET /recipients/{id} -> 404). Ask Flywire to link it to the sandbox key."
    )


def test_unknown_session_confirm_and_expire(flywire):
    fake = str(uuid.uuid4())
    with pytest.raises(NotFoundError):
        flywire.checkout_sessions.confirm(fake)
    assert flywire.checkout_sessions.expire(fake) is ExpireOutcome.NOT_FOUND


def test_malformed_session_id_is_plain_422(flywire):
    with pytest.raises(UnprocessableEntityError) as info:
        flywire.checkout_sessions.expire("not-a-uuid")
    assert not isinstance(info.value, SessionAlreadyCompletedError)


def _session_requests(rid: str, recipient: Recipient | None = None) -> dict[str, CreateCheckoutSessionRequest]:
    common = {"payor_id": "live_test_payer", "external_reference": "live-test", "payor": Payor(country="GB")}
    if recipient is not None:
        common["recipient"] = recipient
    return {
        "one_off_checkout": CreateCheckoutSessionRequest.one_off(amount=LIVE_AMOUNT, recipient_id=rid, **common),
        "one_off_cards": CreateCheckoutSessionRequest.one_off(amount=LIVE_AMOUNT, recipient_id=rid, schema=PaymentSchema.CARDS, **common),
        "recurring": CreateCheckoutSessionRequest.recurring(amount=LIVE_AMOUNT, recipient_id=rid, **common),
        "tokenization": CreateCheckoutSessionRequest.tokenization(
            recipient_id=rid, schema=PaymentSchema.CARDS, mode=ChargeMode.SUBSCRIPTION, **common),
        "tokenization_and_pay": CreateCheckoutSessionRequest.tokenization_and_pay(
            amount=LIVE_AMOUNT, recipient_id=rid, schema=PaymentSchema.CARDS, mode=ChargeMode.INSTALLMENT, **common),
        "preauthorization": CreateCheckoutSessionRequest.preauthorization(amount=LIVE_AMOUNT, recipient_id=rid, **common),
    }


@pytest.mark.parametrize("kind", list(_session_requests("TQQ")))
def test_create_then_expire_each_session_type(flywire, recipient_details, kind):
    if recipient_details is None:
        pytest.skip(f"recipient {CONFIG.recipient_id} not available to this key")
    request = _session_requests(CONFIG.recipient_id, _required_fields(recipient_details))[kind]
    try:
        session = flywire.checkout_sessions.create(request)
    except UnprocessableEntityError as exc:
        # Portal configuration (e.g. recurring not enabled) — report Flywire's reason, don't fail.
        if any("does not support" in str(e.get("message", "")) for e in exc.errors):
            pytest.skip(f"portal {CONFIG.recipient_id}: {exc.errors[0]['message']}")
        raise
    try:
        assert session.id
        assert session.expires_in_seconds and session.expires_in_seconds > 0
        # Confirming before the payer submitted the form must fail.
        with pytest.raises(UnprocessableEntityError):
            flywire.checkout_sessions.confirm(session.id)
    finally:
        assert flywire.checkout_sessions.expire(session.id) in (ExpireOutcome.EXPIRED, ExpireOutcome.NOT_FOUND)


def test_new_mandate_with_unknown_token_rejected(flywire):
    req = CreateCheckoutSessionRequest.new_mandate(
        payment_method_token="a1b2c3d4e5f67890abcd", recipient_id=CONFIG.recipient_id,
        payor_id="live_test_payer", mode=ChargeMode.UNSCHEDULED,
    )
    with pytest.raises(UnprocessableEntityError, match="payment_method_token"):
        flywire.checkout_sessions.create(req)
