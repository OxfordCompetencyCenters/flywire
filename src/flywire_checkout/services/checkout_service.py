"""Application-level checkout orchestration.

Sits between your web layer and :class:`FlywireClient` and enforces the invariants
the raw API cannot: a user may only confirm/expire sessions *they* created, a session
is confirmed at most once, and the amount always comes from the server, never the browser.
"""

from __future__ import annotations

import logging
from typing import Any

from ..client import FlywireClient
from ..exceptions import CheckoutStateError, FlywireAPIError, FlywireError, SessionAlreadyCompletedError
from ..models.sessions import CheckoutSession, CreateCheckoutSessionRequest, ExpireOutcome, SessionConfirmation
from ..resources.checkout_sessions import validate_session_id
from .store import CheckoutStore, SessionRecord, SessionStatus

logger = logging.getLogger("flywire_checkout.service")


class CheckoutService:
    def __init__(self, client: FlywireClient, store: CheckoutStore) -> None:
        self._client = client
        self._store = store

    # ------------------------------------------------------------------ create

    def create_session(self, owner_id: str, request: CreateCheckoutSessionRequest) -> CheckoutSession:
        default_url = self._client.config.notifications_url
        if request.notifications_url is None and default_url:
            request = CreateCheckoutSessionRequest.model_validate(
                {**request.model_dump(by_alias=True), "notifications_url": default_url}
            )

        session = self._client.checkout_sessions.create(request)
        self._store.add_session(
            SessionRecord(
                session_id=session.id,
                owner_id=owner_id,
                session_type=request.type.value,
                amount=request.items[0].amount if request.items else None,
                external_reference=request.external_reference,
            )
        )
        if session.warnings:
            logger.warning(
                "flywire session %s created with %d pre-fill warning(s) on fields: %s",
                session.id, len(session.warnings), ", ".join(w.source or "?" for w in session.warnings),
            )
        return session

    # ------------------------------------------------------------------ confirm

    def confirm_session(self, owner_id: str, confirm_url: str) -> SessionConfirmation:
        """Confirm using the ``confirm_url`` the browser received via postMessage / SDK success event."""
        session_id = self._client.checkout_sessions.session_id_from_confirm_url(confirm_url)
        self._require_owned(owner_id, session_id)

        if not self._store.claim_for_confirmation(session_id):
            raise CheckoutStateError("Session is not awaiting confirmation (already confirmed, in progress or expired)")

        try:
            confirmation = self._client.checkout_sessions.confirm(session_id)
        except FlywireError as exc:
            self._store.update_session(session_id, status=SessionStatus.ERRORED, last_error=_describe(exc))
            raise

        self._store.update_session(
            session_id,
            status=SessionStatus.CONFIRMED,
            payment_reference=confirmation.payment_reference,
            payment_method_token=confirmation.payment_method.token if confirmation.payment_method else None,
            mandate_id=confirmation.mandate.id if confirmation.mandate else None,
            last_error=None,
        )
        logger.info("flywire session %s confirmed (payment_reference=%s)", session_id, confirmation.payment_reference)
        return confirmation

    # ------------------------------------------------------------------ expire

    def expire_session(self, owner_id: str, session_id: str) -> ExpireOutcome:
        record = self._require_owned(owner_id, validate_session_id(session_id))
        if record.status is SessionStatus.CONFIRMED:
            raise CheckoutStateError("Session already confirmed")
        try:
            outcome = self._client.checkout_sessions.expire(session_id)
        except SessionAlreadyCompletedError:
            # Payer already submitted — caller should confirm instead of cancelling.
            raise
        self._store.update_session(session_id, status=SessionStatus.EXPIRED)
        return outcome

    # ------------------------------------------------------------------ frontend errors

    def record_client_error(self, owner_id: str, session_id: str, error: Any) -> None:
        """The SDK's ``error`` event (incl. payer closing the form) must be reported to the backend."""
        self._require_owned(owner_id, validate_session_id(session_id))
        summary = str(error)[:500] if error is not None else "unknown"
        self._store.update_session(session_id, last_error=f"client: {summary}")
        logger.info("flywire session %s reported a client-side error", session_id)

    # ------------------------------------------------------------------ helpers

    def _require_owned(self, owner_id: str, session_id: str) -> SessionRecord:
        record = self._store.get_session(session_id)
        # Same error for "missing" and "not yours" to avoid leaking which session ids exist.
        if record is None or record.owner_id != owner_id:
            raise CheckoutStateError("Unknown checkout session")
        return record


def _describe(exc: FlywireError) -> str:
    if isinstance(exc, FlywireAPIError):
        return f"HTTP {exc.status_code}: {exc.title or ''}".strip()
    return type(exc).__name__
