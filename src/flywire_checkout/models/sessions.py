"""Checkout Session request and response models.

Request models are strict (``extra="forbid"``) so typos fail locally instead of being
silently ignored by the API. Response models allow extra fields, because Flywire
treats new response parameters as a non-breaking change.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_serializer, model_validator

_RECIPIENT_ID_PATTERN = r"^([A-Za-z]{3}|[A-Za-z][A-Za-z0-9]{4})$"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------- enums


class SessionType(str, Enum):
    ONE_OFF = "one_off"
    RECURRING = "recurring"  # Flywire-hosted recurring (installment plans managed by Flywire)
    TOKENIZATION = "tokenization"  # save payment method only
    TOKENIZATION_AND_PAY = "tokenization_and_pay"  # save + charge first payment (also pre-auth)
    NEW_MANDATE = "new_mandate"  # new mandate for an existing token


class PaymentSchema(str, Enum):
    CARDS = "cards"
    DD_SEPA = "dd_sepa"
    DD_BACS = "dd_bacs"
    DD_EFT = "dd_eft"
    DD_ACH = "dd_ach"


class ChargeMode(str, Enum):
    ONE_OFF = "one_off"
    INSTALLMENT = "installment"
    SUBSCRIPTION = "subscription"
    UNSCHEDULED = "unscheduled"


class ActionButton(str, Enum):
    SAVE = "save"
    NEXT = "next"
    PAY = "pay"


PayorFieldName = Literal[
    "first_name", "last_name", "business_name", "address", "city", "country", "state", "phone", "email", "zip"
]


# --------------------------------------------------------------------------- request parts


class Payor(_Request):
    """Pre-fill data for the payer form. All optional; invalid values produce `warnings`, not errors."""

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2")
    state: str | None = Field(default=None, max_length=3, description="Second part of ISO 3166-2, e.g. NY")
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=254)
    zip: str | None = Field(default=None, max_length=20)


class PayorFieldSetting(_Request):
    hidden: bool = False
    read_only: bool = False


class FormOptions(_Request):
    action_button: ActionButton | None = None
    locale: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    show_flywire_logo: bool | None = None
    show_amount_to: bool | None = None
    show_payor_information: bool | None = None
    payor_fields_read_only: bool | None = None
    payor_fields: dict[PayorFieldName, PayorFieldSetting] | None = None


class RecipientField(_Request):
    id: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=255)


class Recipient(_Request):
    """Recipient-specific fields (e.g. student_id). Which ones are required depends on the
    portal: check ``GET /recipients/{id}`` (fields with ``required: true``).

    Sent top-level, as in the parameter tables of the Checkout Sessions reference and the
    "Testing Checkout with API" guide (some code samples there nest it under ``options``).
    """

    fields: list[RecipientField] = Field(default_factory=list)

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "Recipient":
        return cls(fields=[RecipientField(id=k, value=v) for k, v in values.items()])


class SessionOptions(_Request):
    form: FormOptions | None = None


class Item(_Request):
    id: Literal["default"] = "default"
    amount: int = Field(gt=0, description="Amount in currency subunits, e.g. 12025 = 120.25 USD")


class ChargeIntent(_Request):
    mode: ChargeMode
    capture: Literal["manual"] | None = None
    authorization: Literal["preauth", "final"] | None = None
    payment_method_token: str | None = Field(default=None, min_length=1, max_length=64)


# --------------------------------------------------------------------------- create request


class CreateCheckoutSessionRequest(_Request):
    """Body for ``POST /checkout/sessions``.

    Prefer the factory classmethods (:meth:`one_off`, :meth:`recurring`, ...) which
    encode Flywire's per-type rules; the validator below re-checks them regardless.
    """

    type: SessionType
    payment_schema: PaymentSchema | None = Field(default=None, alias="schema")
    charge_intent: ChargeIntent
    payor: Payor | None = None
    options: SessionOptions | None = None
    recipient: Recipient | None = None
    items: list[Item] | None = Field(default=None, min_length=1, max_length=1)
    notifications_url: HttpUrl | None = None
    external_reference: str | None = Field(default=None, min_length=1, max_length=50)
    recipient_id: str = Field(pattern=_RECIPIENT_ID_PATTERN)
    payor_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^\S+$",
        description="Your id for the payer. Required by the API (sandbox returns 422 'payor_id is missing').",
    )
    enable_email_notifications: bool | None = None

    @field_serializer("notifications_url")
    def _url_to_str(self, value: HttpUrl | None) -> str | None:
        return str(value) if value is not None else None

    @model_validator(mode="after")
    def _enforce_type_rules(self) -> "CreateCheckoutSessionRequest":
        t, mode = self.type, self.charge_intent.mode
        if self.notifications_url is not None and self.notifications_url.scheme != "https":
            raise ValueError("notifications_url must use https")

        if t is SessionType.ONE_OFF:
            if mode is not ChargeMode.ONE_OFF:
                raise ValueError("one_off sessions require charge_intent.mode='one_off'")
            if not self.items:
                raise ValueError("one_off sessions require an amount (items)")
        elif t is SessionType.RECURRING:
            if self.payment_schema is not None:
                raise ValueError("recurring (Flywire-hosted) sessions must not set schema")
            if mode is ChargeMode.ONE_OFF:
                raise ValueError("recurring sessions cannot use charge_intent.mode='one_off'")
            if not self.items:
                raise ValueError("recurring sessions require an amount (items)")
        else:  # self-managed recurring: tokenization / tokenization_and_pay / new_mandate
            if self.payment_schema is None:
                raise ValueError(f"{t.value} sessions require schema")
            if mode is ChargeMode.ONE_OFF:
                raise ValueError(f"{t.value} sessions require mode installment, subscription or unscheduled")
            if t is SessionType.TOKENIZATION_AND_PAY and not self.items:
                raise ValueError("tokenization_and_pay sessions require an amount (items)")
            if t is SessionType.NEW_MANDATE and not self.charge_intent.payment_method_token:
                raise ValueError("new_mandate sessions require charge_intent.payment_method_token")
            if t is not SessionType.TOKENIZATION_AND_PAY and self.items:
                raise ValueError(f"{t.value} sessions do not take items")

        if self.charge_intent.capture == "manual":
            if not (t is SessionType.TOKENIZATION_AND_PAY and self.payment_schema is PaymentSchema.CARDS):
                raise ValueError("pre-authorization requires type tokenization_and_pay and schema cards")
        return self

    def to_payload(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    # ---- factories -----------------------------------------------------------------

    @classmethod
    def one_off(
        cls,
        *,
        amount: int,
        recipient_id: str,
        payor_id: str,
        schema: PaymentSchema | None = None,
        **kwargs,
    ) -> "CreateCheckoutSessionRequest":
        """One-off payment. Omit ``schema`` to let the payer pick any method in Checkout Experience."""
        return cls(
            type=SessionType.ONE_OFF,
            payment_schema=schema,
            charge_intent=ChargeIntent(mode=ChargeMode.ONE_OFF),
            items=[Item(amount=amount)],
            recipient_id=recipient_id,
            payor_id=payor_id,
            **kwargs,
        )

    @classmethod
    def recurring(
        cls,
        *,
        amount: int,
        recipient_id: str,
        payor_id: str,
        mode: ChargeMode = ChargeMode.INSTALLMENT,
        **kwargs,
    ) -> "CreateCheckoutSessionRequest":
        """Flywire-hosted recurring plan (plan settings live in Flywire Dashboard)."""
        return cls(
            type=SessionType.RECURRING,
            charge_intent=ChargeIntent(mode=mode),
            items=[Item(amount=amount)],
            recipient_id=recipient_id,
            payor_id=payor_id,
            **kwargs,
        )

    @classmethod
    def tokenization(
        cls,
        *,
        recipient_id: str,
        schema: PaymentSchema,
        mode: ChargeMode,
        payor_id: str,
        **kwargs,
    ) -> "CreateCheckoutSessionRequest":
        """Save a card / bank account for self-managed recurring charges."""
        return cls(
            type=SessionType.TOKENIZATION,
            payment_schema=schema,
            charge_intent=ChargeIntent(mode=mode),
            recipient_id=recipient_id,
            payor_id=payor_id,
            **kwargs,
        )

    @classmethod
    def tokenization_and_pay(
        cls,
        *,
        amount: int,
        recipient_id: str,
        schema: PaymentSchema,
        mode: ChargeMode,
        payor_id: str,
        **kwargs,
    ) -> "CreateCheckoutSessionRequest":
        return cls(
            type=SessionType.TOKENIZATION_AND_PAY,
            payment_schema=schema,
            charge_intent=ChargeIntent(mode=mode),
            items=[Item(amount=amount)],
            recipient_id=recipient_id,
            payor_id=payor_id,
            **kwargs,
        )

    @classmethod
    def preauthorization(
        cls,
        *,
        amount: int,
        recipient_id: str,
        payor_id: str,
        mode: ChargeMode = ChargeMode.UNSCHEDULED,
        authorization: Literal["preauth", "final"] = "preauth",
        **kwargs,
    ) -> "CreateCheckoutSessionRequest":
        """Card pre-authorization (funds held, captured later)."""
        return cls(
            type=SessionType.TOKENIZATION_AND_PAY,
            payment_schema=PaymentSchema.CARDS,
            charge_intent=ChargeIntent(mode=mode, capture="manual", authorization=authorization),
            items=[Item(amount=amount)],
            recipient_id=recipient_id,
            payor_id=payor_id,
            **kwargs,
        )

    @classmethod
    def new_mandate(
        cls,
        *,
        payment_method_token: str,
        recipient_id: str,
        payor_id: str,
        mode: ChargeMode,
        schema: PaymentSchema = PaymentSchema.CARDS,
        **kwargs,
    ) -> "CreateCheckoutSessionRequest":
        return cls(
            type=SessionType.NEW_MANDATE,
            payment_schema=schema,
            charge_intent=ChargeIntent(mode=mode, payment_method_token=payment_method_token),
            recipient_id=recipient_id,
            payor_id=payor_id,
            **kwargs,
        )


# --------------------------------------------------------------------------- responses


class ValidationIssue(_Response):
    text: str | None = None
    error_type: str | None = None


class PrefillWarning(_Response):
    source: str | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)


class HostedForm(_Response):
    url: str
    method: str = "GET"


class CheckoutSession(_Response):
    id: str
    expires_in_seconds: int | None = None
    hosted_form: HostedForm | None = None
    warnings: list[PrefillWarning] = Field(default_factory=list)


class PaymentMethod(_Response):
    token: str | None = None
    type: str | None = None
    card_classification: str | None = None
    last_four_digits: str | None = None
    card_expiration: str | None = None
    brand: str | None = None
    issuer: str | None = None
    country: str | None = None


class MandateAdjustment(_Response):
    category: str | None = None
    proportion: float | None = None
    minimum_fee: int | None = None


class Mandate(_Response):
    id: str | None = None
    currency: str | None = None
    adjustments: list[MandateAdjustment] = Field(default_factory=list)


class ChargeInfo(_Response):
    amount: int | None = None
    currency: str | None = None


class ConfirmedPayor(_Response):
    country: str | None = None
    email: str | None = None
    state: str | None = None


class SessionConfirmation(_Response):
    """Response to ``POST /checkout/sessions/{id}/confirm``.

    * One-off / Flywire-hosted: ``payment_reference`` + ``charge_info``.
    * Tokenization: ``payment_method`` (token) + ``mandate``.
    * tokenization_and_pay: all of the above.
    """

    payment_reference: str | None = None
    charge_info: ChargeInfo | None = None
    payment_method: PaymentMethod | None = None
    mandate: Mandate | None = None
    payor: ConfirmedPayor | None = None


class ExpireOutcome(str, Enum):
    EXPIRED = "expired"
    NOT_FOUND = "not_found"  # already expired / never existed — goal achieved, no action needed
