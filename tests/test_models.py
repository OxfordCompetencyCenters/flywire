import pytest
from pydantic import ValidationError

from flywire_checkout.models import (
    ChargeIntent,
    ChargeMode,
    CreateCheckoutSessionRequest,
    FormOptions,
    PaymentSchema,
    PayorFieldSetting,
    Recipient,
    SessionOptions,
    SessionType,
)


def test_one_off_payload_matches_documented_shape():
    req = CreateCheckoutSessionRequest.one_off(
        amount=33000,
        recipient_id="TQQ",
        schema=PaymentSchema.CARDS,
        external_reference="My payment reference",
        payor_id="my_payer_ID",
        notifications_url="https://example.com/webhooks/flywire",
        options=SessionOptions(form=FormOptions(locale="en", payor_fields={"last_name": PayorFieldSetting(hidden=True)})),
    )
    payload = req.to_payload()
    assert payload["type"] == "one_off"
    assert payload["schema"] == "cards"  # serialised under its API name
    assert payload["charge_intent"] == {"mode": "one_off"}
    assert payload["items"] == [{"id": "default", "amount": 33000}]
    assert payload["notifications_url"] == "https://example.com/webhooks/flywire"
    assert payload["options"]["form"]["payor_fields"]["last_name"] == {"hidden": True, "read_only": False}
    assert "payor" not in payload  # None fields omitted


def test_recurring_omits_schema():
    payload = CreateCheckoutSessionRequest.recurring(payor_id="p1", amount=1000, recipient_id="TQQ").to_payload()
    assert payload["type"] == "recurring" and "schema" not in payload
    with pytest.raises(ValidationError):
        CreateCheckoutSessionRequest(
            type=SessionType.RECURRING, schema="cards", charge_intent=ChargeIntent(mode="installment"),
            items=[{"amount": 1}], recipient_id="TQQ", payor_id="p1",
        )


def test_preauth_rules():
    payload = CreateCheckoutSessionRequest.preauthorization(amount=500, recipient_id="TQQ", payor_id="p1").to_payload()
    assert payload["type"] == "tokenization_and_pay"
    assert payload["charge_intent"]["capture"] == "manual"
    with pytest.raises(ValidationError, match="pre-authorization"):
        CreateCheckoutSessionRequest(
            type=SessionType.TOKENIZATION_AND_PAY, schema="dd_sepa",
            charge_intent=ChargeIntent(mode="unscheduled", capture="manual"),
            items=[{"amount": 1}], recipient_id="TQQ", payor_id="p1",
        )


def test_tokenization_requires_schema_and_recurring_mode():
    req = CreateCheckoutSessionRequest.tokenization(
        recipient_id="TQQ", schema=PaymentSchema.CARDS, mode=ChargeMode.SUBSCRIPTION, payor_id="p1"
    )
    assert "items" not in req.to_payload()
    with pytest.raises(ValidationError):
        CreateCheckoutSessionRequest.tokenization(
            recipient_id="TQQ", schema=PaymentSchema.CARDS, mode=ChargeMode.ONE_OFF, payor_id="p1"
        )


def test_new_mandate_requires_token():
    with pytest.raises(ValidationError):
        CreateCheckoutSessionRequest(
            type="new_mandate", schema="cards", charge_intent=ChargeIntent(mode="subscription"), recipient_id="TQQ", payor_id="p1"
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": 0},
        {"amount": -5},
        {"recipient_id": "1AB"},
        {"recipient_id": "TOOLONG"},
        {"external_reference": "x" * 51},
        {"payor_id": "has space"},
        {"notifications_url": "http://insecure.example.com"},
    ],
)
def test_field_validation(overrides):
    kwargs = {"amount": 100, "recipient_id": "TQQ", "payor_id": "p1", **overrides}
    with pytest.raises(ValidationError):
        CreateCheckoutSessionRequest.one_off(**kwargs)


def test_payor_id_required():
    with pytest.raises(ValidationError, match="payor_id"):
        CreateCheckoutSessionRequest(type="one_off", charge_intent={"mode": "one_off"}, items=[{"amount": 1}], recipient_id="TQQ")


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        CreateCheckoutSessionRequest.one_off(payor_id="p1", amount=100, recipient_id="TQQ", ammount=5)


def test_recipient_fields_are_top_level():
    req = CreateCheckoutSessionRequest.one_off(
        amount=1, recipient_id="D0226", payor_id="p1",
        recipient=Recipient.from_values({"student_first_name": "Ada", "student_email": "ada@example.com"}),
    )
    payload = req.to_payload()
    assert payload["recipient"] == {"fields": [
        {"id": "student_first_name", "value": "Ada"}, {"id": "student_email", "value": "ada@example.com"},
    ]}
    assert "options" not in payload
    with pytest.raises(ValidationError):  # options.recipient is not a supported location
        SessionOptions(recipient={"fields": []})
