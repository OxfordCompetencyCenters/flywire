"""Webhook processing: verify -> parse -> de-duplicate -> dispatch to your handlers.

Flywire retries a notification up to 5 more times if it does not get a 2xx, so:

* return 2xx for anything verified and handled (including duplicates);
* return 401 for signature failures (not retried meaningfully, but logged);
* return 5xx when *your* handler fails, so Flywire re-delivers it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ..exceptions import SignatureVerificationError
from ..models.notifications import PaymentNotification
from ..webhooks import WebhookVerifier
from .store import CheckoutStore

logger = logging.getLogger("flywire_checkout.notifications")

NotificationHandler = Callable[[PaymentNotification], None]
ANY_EVENT = "*"


class ProcessingOutcome(str, Enum):
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"  # bad signature
    INVALID = "invalid"  # signed but unparseable
    FAILED = "failed"  # handler raised — ask Flywire to retry


@dataclass(frozen=True)
class ProcessingResult:
    outcome: ProcessingOutcome
    http_status: int
    notification: PaymentNotification | None = None


class NotificationProcessor:
    def __init__(self, verifier: WebhookVerifier, store: CheckoutStore) -> None:
        self._verifier = verifier
        self._store = store
        self._handlers: dict[str, list[NotificationHandler]] = defaultdict(list)

    def on(self, event_type: str = ANY_EVENT) -> Callable[[NotificationHandler], NotificationHandler]:
        """Decorator: ``@processor.on("guaranteed")`` or ``@processor.on()`` for every event."""

        def register(handler: NotificationHandler) -> NotificationHandler:
            self._handlers[event_type].append(handler)
            return handler

        return register

    def process(self, raw_body: bytes, digest_header: str | None) -> ProcessingResult:
        try:
            notification = self._verifier.parse_payment_notification(raw_body, digest_header)
        except SignatureVerificationError as exc:
            logger.warning("rejected flywire notification: %s", exc)
            return ProcessingResult(ProcessingOutcome.REJECTED, 401)
        except ValueError as exc:
            logger.error("verified flywire notification could not be parsed: %s", exc)
            # 2xx: retrying an unparseable payload will never succeed.
            return ProcessingResult(ProcessingOutcome.INVALID, 202)

        key = notification.idempotency_key
        if not self._store.claim_event(key):
            logger.info("duplicate flywire notification %s ignored", key)
            return ProcessingResult(ProcessingOutcome.DUPLICATE, 200, notification)

        try:
            self._sync_session(notification)
            for handler in (*self._handlers.get(notification.event_type, ()), *self._handlers.get(ANY_EVENT, ())):
                handler(notification)
        except Exception:
            self._store.release_event(key)
            logger.exception("handler failed for flywire notification %s", key)
            return ProcessingResult(ProcessingOutcome.FAILED, 500, notification)

        logger.info("processed flywire notification %s", key)
        return ProcessingResult(ProcessingOutcome.PROCESSED, 200, notification)

    def _sync_session(self, notification: PaymentNotification) -> None:
        record = self._store.find_by_payment_reference(notification.data.payment_id)
        if record is None:
            return
        new_status = notification.data.status or notification.event_type
        # Order by event time, not by status: Flywire documents legitimate "backwards" flows
        # (e.g. cancelled -> initiated -> processed for re-initiated payments), but a retried
        # older event must never overwrite a newer one.
        if record.payment_status_at is not None and notification.event_date < record.payment_status_at:
            logger.info(
                "ignoring stale status %s for payment %s (event %s older than current %s at %s)",
                new_status, notification.data.payment_id, notification.event_date.isoformat(),
                record.payment_status, record.payment_status_at.isoformat(),
            )
            return
        self._store.update_session(
            record.session_id, payment_status=new_status, payment_status_at=notification.event_date
        )
