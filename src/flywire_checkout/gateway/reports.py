"""Payment reports: who paid, how much, when, for what, and how.

Rows combine the gateway's checkout (site, order, what was requested) with Flywire's payment
record (``GET /payments/{id}``, captured on confirmation and on every callback) and the
status history. Reports contain personal data: share them only with people who need them.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..models.notifications import PaymentNotification
from ..models.payments import Payment
from .store import Checkout

# ISO 4217 minor units that differ from 2. Everything else uses 2.
_EXPONENTS = {
    **dict.fromkeys(["BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND",
                     "VUV", "XAF", "XOF", "XPF"], 0),
    **dict.fromkeys(["BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"], 3),
}

BASE_COLUMNS = [
    "site_id", "order_reference", "checkout_id", "description", "checkout_status", "checkout_created_at",
    "payment_reference", "payment_status", "payment_created_at", "guaranteed_at", "delivered_at", "cancelled_at",
    "amount_requested_subunits", "amount_requested", "amount_received", "currency_received", "amount_charged", "currency_charged", "exchange_rate",
    "payer_first_name", "payer_last_name", "payer_email", "payer_country", "payer_state",
    "payment_method", "card_brand", "card_type", "card_last4",
    "status_history", "last_error",
]


def major_units(amount: int | None, currency: str | None) -> str | None:
    """12500 GBP -> "125.00"; 5000 JPY -> "5000"."""
    if amount is None or not currency:
        return None
    exponent = _EXPONENTS.get(currency.upper(), 2)
    return f"{Decimal(amount).scaleb(-exponent):.{exponent}f}"


def snapshot_from_payment(payment: Payment) -> dict[str, Any]:
    """Flatten Flywire's payment record into report fields."""
    t = payment.status_transitions
    method = payment.payment_method_details
    return {
        "payment_reference": payment.payment_id,
        "payment_status": payment.status,
        "payment_created_at": _iso(payment.created_at),
        "guaranteed_at": _iso(t.guaranteed_at),
        "delivered_at": _iso(t.delivered_at),
        "cancelled_at": _iso(t.cancelled_at),
        "amount_to": payment.amount_to,
        "currency_to": payment.currency_to,
        "amount_from": payment.amount_from,
        "currency_from": payment.currency_from,
        "exchange_rate": payment.exchange_rate,
        "payer_email": payment.payer.email,
        "payer_country": payment.payer.country,
        "payer_state": payment.payer.state,
        "payer_first_name": payment.payer.first_name,
        "payer_last_name": payment.payer.last_name,
        "payment_method": method.type,
        "card_brand": method.brand,
        "card_type": method.card_classification,
        "card_last4": method.last_four_digits,
        "recipient_fields": payment.recipient_field_values(),
    }


def snapshot_from_notification(notification: PaymentNotification) -> dict[str, Any]:
    """Fields a callback carries. Payer name/address only if Flywire enabled the payer object."""
    data = notification.data
    payer = data.payer
    method = data.payment_method
    return {
        "payment_reference": data.payment_id,
        "payment_status": data.status or notification.event_type,
        "amount_to": _int(data.amount_to),
        "currency_to": data.currency_to,
        "amount_from": _int(data.amount_from),
        "currency_from": data.currency_from,
        "payer_country": data.country,
        "payer_first_name": payer.first_name if payer else None,
        "payer_last_name": payer.last_name if payer else None,
        "payer_email": payer.email if payer else None,
        "payment_method": method.type if method else None,
        "card_brand": method.brand if method else None,
        "card_type": method.card_classification if method else None,
        "card_last4": method.last_four_digits if method else None,
    }


def build_rows(
    checkouts: list[Checkout],
    details: dict[str, dict[str, Any]],
    events: dict[str, list[dict[str, Any]]],
    field_names_by_site: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (rows, columns). Student/portal columns are the fields each site sends."""
    student_columns: list[str] = []
    rows = []
    for checkout in checkouts:
        d = details.get(checkout.id, {})
        wanted = field_names_by_site.get(checkout.site_id) or list(checkout.recipient_fields)
        flywire_fields = d.get("recipient_fields") or {}
        student = {name: flywire_fields.get(name) or checkout.recipient_fields.get(name) for name in wanted}
        for name in student:
            if name not in student_columns:
                student_columns.append(name)
        history = "; ".join(f"{e['status'] or e['event_type']}@{e['event_date']}" for e in events.get(checkout.id, []))
        rows.append({
            "site_id": checkout.site_id,
            "order_reference": checkout.order_reference,
            "checkout_id": checkout.id,
            "description": checkout.description,
            "checkout_status": checkout.status.value,
            "checkout_created_at": _iso(checkout.created_at),
            "payment_reference": d.get("payment_reference") or checkout.payment_reference,
            "payment_status": d.get("payment_status") or checkout.payment_status,
            "payment_created_at": d.get("payment_created_at"),
            "guaranteed_at": d.get("guaranteed_at"),
            "delivered_at": d.get("delivered_at"),
            "cancelled_at": d.get("cancelled_at"),
            # Always in subunits (pence); the major-unit form needs the currency, known once paid.
            "amount_requested_subunits": checkout.amount,
            "amount_requested": major_units(checkout.amount, d.get("currency_to")),
            "amount_received": major_units(d.get("amount_to"), d.get("currency_to")),
            "currency_received": d.get("currency_to"),
            "amount_charged": major_units(d.get("amount_from"), d.get("currency_from")),
            "currency_charged": d.get("currency_from"),
            "exchange_rate": d.get("exchange_rate"),
            "payer_first_name": d.get("payer_first_name"),
            "payer_last_name": d.get("payer_last_name"),
            "payer_email": d.get("payer_email"),
            "payer_country": d.get("payer_country"),
            "payer_state": d.get("payer_state"),
            "payment_method": d.get("payment_method"),
            "card_brand": d.get("card_brand"),
            "card_type": d.get("card_type"),
            "card_last4": d.get("card_last4"),
            "status_history": history or None,
            "last_error": checkout.last_error,
            **{f"field_{name}": value for name, value in student.items()},
        })
    return rows, BASE_COLUMNS + [f"field_{name}" for name in student_columns]


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    # Spreadsheet formula injection: payer-entered text like "=HYPERLINK(...)" must stay text.
    if text.startswith(_FORMULA_PREFIXES):
        text = "'" + text
    return text


def to_csv(rows: list[dict[str, Any]], columns: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    return buffer.getvalue()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _int(value: Any) -> int | None:
    try:
        return int(str(value)) if value not in (None, "") else None
    except ValueError:
        return None
