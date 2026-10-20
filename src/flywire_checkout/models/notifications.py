"""Payment Status Notification (callback/webhook) models.

Flywire may add notification types and payload fields without a version bump,
so ``event_type`` / ``status`` stay plain strings with enum helpers alongside.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PaymentStatus(str, Enum):
    INITIATED = "initiated"
    AUTHORIZED = "authorized"  # pre-authorization only
    ADJUSTED = "adjusted"  # pre-authorization only
    PROCESSED = "processed"
    GUARANTEED = "guaranteed"
    DELIVERED = "delivered"  # final
    REVERSED = "reversed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def try_parse(cls, value: str | None) -> "PaymentStatus | None":
        try:
            return cls(value) if value else None
        except ValueError:
            return None


class CancellationReason(str, Enum):
    CANCELLED_BY_USER = "cancelled_by_user"
    REJECTED = "rejected"
    DUPLICATED = "duplicated"
    EXPIRED = "expired"
    UNPAID = "unpaid"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="allow")


class NotificationPaymentMethod(_Payload):
    type: str | None = None
    brand: str | None = None
    card_classification: str | None = None
    card_expiration: str | None = None
    last_four_digits: str | None = None


class NotificationPayer(_Payload):
    first_name: str | None = None
    last_name: str | None = None
    middle_name: str | None = None
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    zip: str | None = None
    country: str | None = None
    state: str | None = None
    phone: str | None = None
    email: str | None = None


class PaymentNotificationData(_Payload):
    payment_id: str
    status: str | None = None
    amount_from: str | None = None
    currency_from: str | None = None
    amount_to: str | None = None
    currency_to: str | None = None
    expiration_date: datetime | None = None
    external_reference: str | None = None
    country: str | None = None
    payment_method: NotificationPaymentMethod | None = None
    recurring_id: str | None = None
    cancellation_reason: str | None = None
    reason: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    payer: NotificationPayer | None = None


class PaymentNotification(_Payload):
    event_type: str
    event_date: datetime
    event_resource: str | None = None  # "payments" | "charges"
    data: PaymentNotificationData

    @property
    def status(self) -> PaymentStatus | None:
        return PaymentStatus.try_parse(self.data.status or self.event_type)

    @property
    def idempotency_key(self) -> str:
        """Stable key for de-duplicating Flywire retries (up to 6 deliveries per event)."""
        return f"{self.data.payment_id}:{self.event_resource or ''}:{self.event_type}:{self.event_date.isoformat()}"
