"""Persistence port for checkout state.

:class:`CheckoutStore` is a Protocol: implement it on top of your platform's database
(SQLAlchemy, Django ORM, Redis...). Implementations MUST make ``claim_*`` methods atomic
(e.g. ``UPDATE ... WHERE status = 'created'`` / ``INSERT ... ON CONFLICT DO NOTHING``),
because they are what prevents double-confirmation and double-processing of webhooks.

:class:`InMemoryCheckoutStore` is thread-safe and suitable for tests and local demos only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol


class SessionStatus(str, Enum):
    CREATED = "created"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    ERRORED = "errored"


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    owner_id: str
    session_type: str
    amount: int | None
    external_reference: str | None
    status: SessionStatus = SessionStatus.CREATED
    payment_reference: str | None = None
    payment_method_token: str | None = None
    mandate_id: str | None = None
    payment_status: str | None = None
    payment_status_at: datetime | None = None
    """event_date of the notification that set payment_status (used to drop stale retries)."""
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CheckoutStore(Protocol):
    def add_session(self, record: SessionRecord) -> None: ...

    def get_session(self, session_id: str) -> SessionRecord | None: ...

    def update_session(self, session_id: str, **changes: object) -> SessionRecord: ...

    def claim_for_confirmation(self, session_id: str) -> bool:
        """Atomically move CREATED/ERRORED -> CONFIRMING. Return False if not in a claimable state."""
        ...

    def find_by_payment_reference(self, payment_reference: str) -> SessionRecord | None: ...

    def claim_event(self, idempotency_key: str) -> bool:
        """Atomically record a webhook event key. Return False if already recorded."""
        ...

    def release_event(self, idempotency_key: str) -> None:
        """Forget an event key so a Flywire retry can be processed again (handler failed)."""
        ...


class InMemoryCheckoutStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionRecord] = {}
        self._events: set[str] = set()

    def add_session(self, record: SessionRecord) -> None:
        with self._lock:
            if record.session_id in self._sessions:
                raise ValueError("session already exists")
            self._sessions[record.session_id] = record

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            return self._sessions.get(session_id)

    def update_session(self, session_id: str, **changes: object) -> SessionRecord:
        with self._lock:
            current = self._sessions[session_id]
            updated = replace(current, updated_at=datetime.now(timezone.utc), **changes)
            self._sessions[session_id] = updated
            return updated

    def claim_for_confirmation(self, session_id: str) -> bool:
        with self._lock:
            current = self._sessions.get(session_id)
            if current is None or current.status not in (SessionStatus.CREATED, SessionStatus.ERRORED):
                return False
            self._sessions[session_id] = replace(
                current, status=SessionStatus.CONFIRMING, updated_at=datetime.now(timezone.utc)
            )
            return True

    def find_by_payment_reference(self, payment_reference: str) -> SessionRecord | None:
        with self._lock:
            return next((r for r in self._sessions.values() if r.payment_reference == payment_reference), None)

    def claim_event(self, idempotency_key: str) -> bool:
        with self._lock:
            if idempotency_key in self._events:
                return False
            self._events.add(idempotency_key)
            return True

    def release_event(self, idempotency_key: str) -> None:
        with self._lock:
            self._events.discard(idempotency_key)
