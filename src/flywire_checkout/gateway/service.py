"""Gateway business logic: one Flywire integration shared by many websites."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..client import FlywireClient
from ..exceptions import FlywireError, SessionAlreadyCompletedError, SignatureVerificationError
from ..models.notifications import PaymentNotification
from ..models.sessions import CreateCheckoutSessionRequest, FormOptions, Payor, Recipient, SessionOptions
from ..webhooks import WebhookVerifier
from .events import EventDeliveryError, EventForwarder, event_id_for
from .reports import build_rows, snapshot_from_notification, snapshot_from_payment
from .settings import GatewaySettings
from .sites import SiteConfig, SiteRegistry
from .store import Checkout, CheckoutStatus, GatewayStore

logger = logging.getLogger("flywire_checkout.gateway")

_CHECKOUT_ID = re.compile(r"^chk_[A-Za-z0-9_-]{24}$")
# Refresh the Flywire session if it has less than this left, so the payer isn't cut off mid-form.
_SESSION_REFRESH_MARGIN = timedelta(minutes=5)
# Max Flywire lookups per report request (Flywire allows 500 requests/minute per IP).
_REFRESH_LIMIT = 100


# --------------------------------------------------------------------------- errors


class GatewayError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(GatewayError):
    status_code, code = 404, "not_found"


class Conflict(GatewayError):
    status_code, code = 409, "conflict"


class Gone(GatewayError):
    status_code, code = 410, "gone"


class InvalidRequest(GatewayError):
    status_code, code = 422, "invalid_request"


class UpstreamError(GatewayError):
    status_code, code = 502, "payment_provider_error"


# --------------------------------------------------------------------------- input


class PayerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^\S+$")
    """Your stable id for the payer (reused for repeat payments). Defaults to a per-checkout id."""
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    zip: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    state: str | None = Field(default=None, max_length=3)


class CreateCheckoutInput(BaseModel):
    """What a website sends to ``POST /v1/checkouts``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    order_reference: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    """The site's own order/invoice id. Unique per site — repeating it returns the same checkout."""
    amount: int = Field(gt=0, description="In the portal currency's subunits, e.g. 12025 = £120.25")
    session_type: str = "one_off"
    description: str | None = Field(default=None, max_length=200)
    payer: PayerInput = Field(default_factory=PayerInput)
    recipient_fields: dict[str, str] = Field(default_factory=dict, max_length=30)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("recipient_fields", "metadata")
    @classmethod
    def _bounded(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key):
                raise ValueError(f"invalid key {key!r}")
            if len(item) > 255:
                raise ValueError(f"value for {key!r} is too long")
        return value


# --------------------------------------------------------------------------- service


class GatewayService:
    def __init__(
        self,
        *,
        settings: GatewaySettings,
        sites: SiteRegistry,
        client: FlywireClient,
        store: GatewayStore,
        forwarder: EventForwarder,
        verifier: WebhookVerifier | None,
    ) -> None:
        self.settings = settings
        self.sites = sites
        self.client = client
        self.store = store
        self.forwarder = forwarder
        self.verifier = verifier
        self._secret = settings.secret.get_secret_value().encode()

    # ------------------------------------------------------------------ links

    def payment_token(self, checkout_id: str) -> str:
        mac = hmac.new(self._secret, f"pay:{checkout_id}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    def payment_url(self, checkout_id: str) -> str:
        return f"{self.settings.public_url}/pay/{checkout_id}/{self.payment_token(checkout_id)}"

    def resolve_link(self, checkout_id: str, token: str) -> tuple[Checkout, SiteConfig]:
        if not _CHECKOUT_ID.fullmatch(checkout_id or "") or not hmac.compare_digest(
            self.payment_token(checkout_id).encode(), (token or "").encode()
        ):
            raise NotFound("Payment link not found")
        checkout = self.store.get(checkout_id)
        site = self.sites.get(checkout.site_id) if checkout else None
        if checkout is None or site is None:
            raise NotFound("Payment link not found")
        return self._expire_if_due(checkout), site

    def _expire_if_due(self, checkout: Checkout) -> Checkout:
        if not checkout.status.is_final and datetime.now(timezone.utc) >= checkout.expires_at:
            if self.store.transition(
                checkout.id, (CheckoutStatus.CREATED, CheckoutStatus.OPEN, CheckoutStatus.ERRORED), CheckoutStatus.EXPIRED
            ):
                checkout = self.store.get(checkout.id) or checkout
        return checkout

    # ------------------------------------------------------------------ site API

    def create_checkout(self, site: SiteConfig, data: CreateCheckoutInput) -> tuple[Checkout, bool]:
        if data.session_type not in site.session_types:
            raise InvalidRequest(f"session_type {data.session_type!r} is not enabled for this site")
        if site.max_amount is not None and data.amount > site.max_amount:
            raise InvalidRequest(f"amount exceeds this site's limit of {site.max_amount}")
        missing = [f for f in site.required_fields if not data.recipient_fields.get(f, "").strip()]
        if missing:
            raise InvalidRequest(f"recipient_fields missing required values: {', '.join(missing)}")
        if not (site.recipient_id or self.client.config.recipient_id):
            raise InvalidRequest("no Flywire recipient configured for this site")

        candidate = Checkout(
            id="chk_" + secrets.token_urlsafe(18),
            site_id=site.id,
            order_reference=data.order_reference,
            amount=data.amount,
            session_type=data.session_type,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=self.settings.checkout_ttl_hours),
            payer=data.payer.model_dump(exclude_none=True),
            recipient_fields=data.recipient_fields,
            description=data.description,
            metadata=data.metadata,
        )
        checkout, created = self.store.create_or_get(candidate)
        if not created and (checkout.amount != data.amount or checkout.session_type != data.session_type):
            raise Conflict("order_reference already used with a different amount or session_type")
        logger.info("checkout %s %s for site %s order %s", checkout.id, "created" if created else "reused",
                    site.id, data.order_reference)
        return self._expire_if_due(checkout), created

    def get_checkout(self, site: SiteConfig, checkout_id: str) -> Checkout:
        checkout = self.store.get(checkout_id) if _CHECKOUT_ID.fullmatch(checkout_id or "") else None
        if checkout is None or checkout.site_id != site.id:
            raise NotFound("Checkout not found")
        return self._expire_if_due(checkout)

    def cancel_checkout(self, site: SiteConfig, checkout_id: str) -> Checkout:
        checkout = self.get_checkout(site, checkout_id)
        if checkout.status in (CheckoutStatus.CANCELLED, CheckoutStatus.EXPIRED):
            return checkout
        if checkout.status in (CheckoutStatus.CONFIRMED, CheckoutStatus.CONFIRMING):
            raise Conflict("The payer already submitted this checkout")
        if checkout.flywire_session_id:
            try:
                self.client.checkout_sessions.expire(checkout.flywire_session_id)
            except SessionAlreadyCompletedError:
                raise Conflict("The payer already submitted this checkout; it will be confirmed") from None
            except FlywireError as exc:
                raise UpstreamError(f"Could not cancel the payment session: {_brief(exc)}") from exc
        if not self.store.transition(
            checkout.id, (CheckoutStatus.CREATED, CheckoutStatus.OPEN, CheckoutStatus.ERRORED), CheckoutStatus.CANCELLED
        ):
            raise Conflict("Checkout changed state; fetch it again")
        return self.store.get(checkout.id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ payer (browser) API

    def open_payment(self, checkout_id: str, token: str) -> tuple[Checkout, SiteConfig]:
        """Called when the payer opens the payment page. Ensures a live Flywire session."""
        checkout, site = self.resolve_link(checkout_id, token)
        if checkout.status is CheckoutStatus.CONFIRMED:
            return checkout, site
        if checkout.status in (CheckoutStatus.CANCELLED, CheckoutStatus.EXPIRED):
            raise Gone(f"This payment link is {checkout.status.value}")
        if checkout.status is CheckoutStatus.CONFIRMING:
            raise Conflict("Payment is being confirmed; refresh in a moment")

        now = datetime.now(timezone.utc)
        if checkout.flywire_session_id and checkout.flywire_session_expires_at and \
                checkout.flywire_session_expires_at - now > _SESSION_REFRESH_MARGIN:
            return checkout, site

        try:
            session = self.client.checkout_sessions.create(self._session_request(checkout, site))
        except FlywireError as exc:
            logger.error("flywire session creation failed for %s: %s", checkout.id, exc)
            self.store.update(checkout.id, last_error=f"create_session: {_brief(exc)}")
            raise UpstreamError("The payment provider rejected the request") from exc

        if session.warnings:
            logger.warning("checkout %s: pre-fill warnings on %s", checkout.id,
                           ", ".join(w.source or "?" for w in session.warnings))
        checkout = self.store.update(
            checkout.id,
            status=CheckoutStatus.OPEN,
            flywire_session_id=session.id,
            flywire_session_expires_at=now + timedelta(seconds=session.expires_in_seconds or 1800),
            hosted_form_url=session.hosted_form.url if session.hosted_form else None,
            last_error=None,
        )
        return checkout, site

    def confirm_payment(self, checkout_id: str, token: str, confirm_url: str) -> Checkout:
        checkout, site = self.resolve_link(checkout_id, token)
        try:
            session_id = self.client.checkout_sessions.session_id_from_confirm_url(confirm_url)
        except FlywireError as exc:
            raise InvalidRequest("Invalid confirmation URL") from exc
        if session_id != checkout.flywire_session_id:
            # Never confirm a session that isn't the one this checkout opened.
            raise InvalidRequest("Confirmation does not match this checkout")
        if checkout.status is CheckoutStatus.CONFIRMED:
            return checkout
        if not self.store.transition(checkout.id, (CheckoutStatus.OPEN, CheckoutStatus.ERRORED), CheckoutStatus.CONFIRMING):
            raise Conflict("Checkout is not awaiting confirmation")

        try:
            confirmation = self.client.checkout_sessions.confirm(session_id)
        except FlywireError as exc:
            self.store.update(checkout.id, status=CheckoutStatus.ERRORED, last_error=f"confirm: {_brief(exc)}")
            raise UpstreamError("Could not confirm the payment") from exc

        checkout = self.store.update(
            checkout.id,
            status=CheckoutStatus.CONFIRMED,
            payment_reference=confirmation.payment_reference or checkout.payment_reference,
            last_error=None,
        )
        self.capture_payment_details(checkout)
        self._deliver_best_effort(site, event_id_for(checkout.id, "submitted"), "checkout.submitted", checkout)
        return checkout

    def record_client_error(self, checkout_id: str, token: str, error: Any) -> None:
        checkout, _ = self.resolve_link(checkout_id, token)
        self.store.update(checkout.id, last_error=f"client: {str(error)[:300]}")

    # ------------------------------------------------------------------ Flywire webhooks

    def handle_flywire_notification(self, raw_body: bytes, digest: str | None) -> int:
        """Returns the HTTP status to answer Flywire with (5xx makes Flywire retry)."""
        if self.verifier is None:
            logger.error("received a Flywire notification but no shared secret is configured")
            return 503
        try:
            notification = self.verifier.parse_payment_notification(raw_body, digest)
        except SignatureVerificationError as exc:
            logger.warning("rejected flywire notification: %s", exc)
            return 401
        except ValueError as exc:
            logger.error("unparseable flywire notification: %s", exc)
            return 202

        # Scope de-duplication to the checkout too, so two checkouts can never collide.
        key = f"{notification.idempotency_key}:{notification.data.external_reference or ''}"
        if not self.store.claim_event(key):
            return 200
        try:
            self._apply_notification(notification)
        except EventDeliveryError as exc:
            self.store.release_event(key)
            logger.warning("could not forward %s to site (%s); Flywire will retry", key, exc)
            return 503
        except Exception:
            self.store.release_event(key)
            logger.exception("failed processing flywire notification %s", key)
            return 500
        return 200

    def _apply_notification(self, notification: PaymentNotification) -> None:
        ref = notification.data.external_reference or ""
        checkout = self.store.get(ref) if _CHECKOUT_ID.fullmatch(ref) else None
        if checkout is None:
            logger.info("flywire notification for unknown reference %r ignored", ref)
            return
        site = self.sites.get(checkout.site_id)
        new_status = notification.data.status or notification.event_type
        changes: dict[str, Any] = {}
        if not checkout.payment_reference:
            changes["payment_reference"] = notification.data.payment_id
        # Order by event time: Flywire has legitimate "backwards" flows (cancelled -> initiated).
        if checkout.payment_status_at is None or notification.event_date >= checkout.payment_status_at:
            changes.update(payment_status=new_status, payment_status_at=notification.event_date)
        if changes:
            checkout = self.store.update(checkout.id, **changes)
        self.store.add_payment_event(checkout.id, notification.idempotency_key, notification.event_type,
                                     notification.data.status, notification.event_date)
        self.store.save_payment_details(checkout.id, notification.data.payment_id,
                                        snapshot_from_notification(notification))
        self.capture_payment_details(checkout)
        if site is not None:
            self.forwarder.deliver(
                site,
                event_id_for(notification.idempotency_key, checkout.id),
                "payment.status_changed",
                {
                    **public_view(checkout),
                    "flywire_event": {
                        "type": notification.event_type,
                        "date": notification.event_date.isoformat(),
                        "status": notification.data.status,
                        "amount_from": notification.data.amount_from,
                        "currency_from": notification.data.currency_from,
                        "amount_to": notification.data.amount_to,
                        "currency_to": notification.data.currency_to,
                        "payment_method": (notification.data.payment_method.type
                                           if notification.data.payment_method else None),
                        "cancellation_reason": notification.data.cancellation_reason,
                    },
                },
            )

    # ------------------------------------------------------------------ reporting

    def capture_payment_details(self, checkout: Checkout) -> bool:
        """Fetch Flywire's record of the payment and store it for reports. Best effort:
        never let reporting break confirmation or callback handling."""
        if not checkout.payment_reference:
            return False
        try:
            payment = self.client.payments.get(checkout.payment_reference)
        except (FlywireError, ValueError) as exc:
            logger.warning("could not fetch payment details for %s: %s", checkout.id, _brief(exc))
            return False
        self.store.save_payment_details(checkout.id, payment.payment_id, snapshot_from_payment(payment))
        return True

    def payment_report(
        self,
        site: SiteConfig | None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        *,
        statuses: set[str] | None = None,
        refresh: bool = False,
        limit: int = 5000,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Rows for one site (or all sites when ``site`` is None), newest first."""
        checkouts = self.store.list_checkouts(site.id if site else None, created_from, created_to, limit)
        if refresh:
            # Refresh records without details (bounded, to stay well inside Flywire's rate limit).
            known = self.store.get_payment_details([c.id for c in checkouts])
            for checkout in [c for c in checkouts if c.payment_reference and c.id not in known][:_REFRESH_LIMIT]:
                self.capture_payment_details(checkout)
        details = self.store.get_payment_details([c.id for c in checkouts])
        events = self.store.get_payment_events([c.id for c in checkouts])
        fields = {s.id: s.required_fields for s in self.sites}
        rows, columns = build_rows(checkouts, details, events, fields)
        if statuses:
            rows = [r for r in rows if (r["payment_status"] or r["checkout_status"]) in statuses]
        return rows, columns

    # ------------------------------------------------------------------ helpers

    def _session_request(self, checkout: Checkout, site: SiteConfig) -> CreateCheckoutSessionRequest:
        payer = dict(checkout.payer)
        payor_id = payer.pop("id", None) or f"{site.id}_{checkout.order_reference}"[:100]
        payor_id = re.sub(r"\s+", "_", payor_id)
        common: dict[str, Any] = {
            "amount": checkout.amount,
            "recipient_id": site.recipient_id or self.client.config.recipient_id,
            "payor_id": payor_id,
            "external_reference": checkout.id,
            "payor": Payor(**payer) if payer else None,
            "recipient": Recipient.from_values(checkout.recipient_fields) if checkout.recipient_fields else None,
            "options": SessionOptions(form=FormOptions(locale=site.locale)),
        }
        if self.settings.is_https:
            common["notifications_url"] = f"{self.settings.public_url}/webhooks/flywire"
        common = {k: v for k, v in common.items() if v is not None}
        if checkout.session_type == "recurring":
            return CreateCheckoutSessionRequest.recurring(**common)
        return CreateCheckoutSessionRequest.one_off(**common)

    def _deliver_best_effort(self, site: SiteConfig, event_id: str, event_type: str, checkout: Checkout) -> None:
        try:
            self.forwarder.deliver(site, event_id, event_type, public_view(checkout))
        except EventDeliveryError as exc:
            # The payer flow must not fail because a site is down; sites can also poll GET /v1/checkouts/{id}.
            logger.warning("could not deliver %s for %s: %s", event_type, checkout.id, exc)


def public_view(checkout: Checkout) -> dict[str, Any]:
    """What sites see. Never includes Flywire session ids or links."""
    return {
        "checkout_id": checkout.id,
        "order_reference": checkout.order_reference,
        "status": checkout.status.value,
        "amount": checkout.amount,
        "session_type": checkout.session_type,
        "payment_reference": checkout.payment_reference,
        "payment_status": checkout.payment_status,
        "payment_status_at": checkout.payment_status_at.isoformat() if checkout.payment_status_at else None,
        "metadata": checkout.metadata,
        "expires_at": checkout.expires_at.isoformat(),
        "created_at": checkout.created_at.isoformat(),
        "updated_at": checkout.updated_at.isoformat(),
    }


def _brief(exc: Exception) -> str:
    return str(exc)[:300]
