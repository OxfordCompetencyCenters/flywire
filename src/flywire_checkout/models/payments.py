"""``GET /payments/{paymentID}`` — Flywire's real-time record of one payment.

Modelled on Flywire's documented response and a live sandbox payment (Oct 2026). Extra
fields are allowed because Flywire may add response parameters without a version bump.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class StatusTransitions(_Model):
    guaranteed_at: datetime | None = None
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None


class PaymentPayer(_Model):
    email: str | None = None
    country: str | None = None
    state: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class PaymentRecipientField(_Model):
    id: str
    value: str | None = None


class PaymentRecipient(_Model):
    id: str | None = None
    fields: list[PaymentRecipientField] = Field(default_factory=list)


class PaymentMethodDetails(_Model):
    type: str | None = None
    name: str | None = None
    brand: str | None = None
    card_classification: str | None = None
    card_expiration: str | None = None
    last_four_digits: str | None = None
    status: str | None = None


class Payout(_Model):
    recipient_id: str | None = None
    reference: str | None = None
    status: str | None = None
    amount: int | None = None
    currency: str | None = None


class ChargeIntentDetails(_Model):
    initiator: str | None = None
    mode: str | None = None
    payor_id: str | None = None


class Payment(_Model):
    payment_id: str
    status: str | None = None
    created_at: datetime | None = None
    expiration_date: datetime | None = None
    status_transitions: StatusTransitions = Field(default_factory=StatusTransitions)
    exchange_rate: float | None = None
    amount_from: int | None = None
    currency_from: str | None = None
    amount_to: int | None = None
    currency_to: str | None = None
    payer: PaymentPayer = Field(default_factory=PaymentPayer)
    recipient: PaymentRecipient = Field(default_factory=PaymentRecipient)
    payment_method_details: PaymentMethodDetails = Field(default_factory=PaymentMethodDetails)
    payouts: list[Payout] = Field(default_factory=list)
    charge_intent: ChargeIntentDetails = Field(default_factory=ChargeIntentDetails)
    external_reference: str | None = None

    def recipient_field_values(self) -> dict[str, str]:
        """Non-empty portal fields (e.g. student_first_name) as a dict."""
        return {f.id: f.value for f in self.recipient.fields if f.value not in (None, "")}
