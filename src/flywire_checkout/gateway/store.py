"""Gateway persistence.

:class:`GatewayStore` is the port; :class:`SqliteGatewayStore` is a production-capable
implementation for a single gateway instance (WAL mode, serialised writes, atomic state
transitions). For several instances behind a load balancer, implement the same Protocol
on PostgreSQL — every state change below is a single conditional UPDATE / INSERT.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class CheckoutStatus(str, Enum):
    CREATED = "created"  # registered by the site; no Flywire session yet
    OPEN = "open"  # Flywire session created, payer can fill the form
    CONFIRMING = "confirming"  # confirm call in flight
    CONFIRMED = "confirmed"  # payer submitted, session confirmed — await payment events
    ERRORED = "errored"  # confirm failed; may be retried
    CANCELLED = "cancelled"  # cancelled by the site
    EXPIRED = "expired"  # checkout link lifetime passed

    @property
    def is_final(self) -> bool:
        return self in (CheckoutStatus.CONFIRMED, CheckoutStatus.CANCELLED, CheckoutStatus.EXPIRED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Checkout:
    id: str
    site_id: str
    order_reference: str
    amount: int
    session_type: str
    expires_at: datetime
    payer: dict[str, Any] = field(default_factory=dict)
    recipient_fields: dict[str, str] = field(default_factory=dict)
    description: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    status: CheckoutStatus = CheckoutStatus.CREATED
    flywire_session_id: str | None = None
    flywire_session_expires_at: datetime | None = None
    hosted_form_url: str | None = None
    payment_reference: str | None = None
    payment_status: str | None = None
    payment_status_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


class GatewayStore(Protocol):
    def create_or_get(self, checkout: Checkout) -> tuple[Checkout, bool]:
        """Insert, or return the existing checkout for (site_id, order_reference). Bool = created."""
        ...

    def get(self, checkout_id: str) -> Checkout | None: ...

    def update(self, checkout_id: str, **changes: Any) -> Checkout: ...

    def transition(self, checkout_id: str, from_statuses: tuple[CheckoutStatus, ...], to: CheckoutStatus) -> bool:
        """Atomically move to ``to`` only if currently in one of ``from_statuses``."""
        ...

    def claim_event(self, key: str) -> bool: ...

    def release_event(self, key: str) -> None: ...


# --------------------------------------------------------------------------- SQLite

_JSON_FIELDS = {"payer", "recipient_fields", "metadata"}
_DT_FIELDS = {"expires_at", "flywire_session_expires_at", "payment_status_at", "created_at", "updated_at"}
_COLUMNS = [f.name for f in fields(Checkout)]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS checkouts (
    {", ".join(f"{c} TEXT" if c != "amount" else "amount INTEGER NOT NULL" for c in _COLUMNS)},
    PRIMARY KEY (id),
    UNIQUE (site_id, order_reference)
);
CREATE TABLE IF NOT EXISTS processed_events (
    key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


def _encode(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _JSON_FIELDS:
        return json.dumps(value, separators=(",", ":"))
    if name in _DT_FIELDS:
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _decode(row: sqlite3.Row) -> Checkout:
    data: dict[str, Any] = {}
    for name in _COLUMNS:
        value = row[name]
        if value is not None and name in _JSON_FIELDS:
            value = json.loads(value)
        elif value is not None and name in _DT_FIELDS:
            value = datetime.fromisoformat(value)
        elif name == "status":
            value = CheckoutStatus(value)
        data[name] = value
    for name in _JSON_FIELDS:
        data[name] = data[name] or {}
    return Checkout(**data)


class SqliteGatewayStore:
    def __init__(self, path: str | Path = "gateway.db") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self._path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create_or_get(self, checkout: Checkout) -> tuple[Checkout, bool]:
        values = [_encode(c, getattr(checkout, c)) for c in _COLUMNS]
        # Column names below come from the Checkout dataclass, never from input; values are bound.
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._lock:
            cur = self._db.execute(
                f"INSERT OR IGNORE INTO checkouts ({', '.join(_COLUMNS)}) VALUES ({placeholders})", values  # noqa: S608
            )
            created = cur.rowcount == 1
            row = self._db.execute(
                "SELECT * FROM checkouts WHERE site_id = ? AND order_reference = ?",
                (checkout.site_id, checkout.order_reference),
            ).fetchone()
        return _decode(row), created

    def get(self, checkout_id: str) -> Checkout | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
        return _decode(row) if row else None

    def update(self, checkout_id: str, **changes: Any) -> Checkout:
        unknown = set(changes) - set(_COLUMNS)
        if unknown or "id" in changes:
            raise ValueError(f"cannot update fields: {sorted(unknown | ({'id'} & set(changes)))}")
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in changes)
        with self._lock:
            cur = self._db.execute(
                f"UPDATE checkouts SET {assignments} WHERE id = ?",  # noqa: S608 (keys validated above)
                [_encode(k, v) for k, v in changes.items()] + [checkout_id],
            )
            if cur.rowcount != 1:
                raise KeyError(checkout_id)
            row = self._db.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
        return _decode(row)

    def transition(self, checkout_id: str, from_statuses: tuple[CheckoutStatus, ...], to: CheckoutStatus) -> bool:
        marks = ", ".join("?" for _ in from_statuses)
        with self._lock:
            cur = self._db.execute(
                f"UPDATE checkouts SET status = ?, updated_at = ? WHERE id = ? AND status IN ({marks})",  # noqa: S608
                [to.value, _encode("updated_at", _now()), checkout_id, *(s.value for s in from_statuses)],
            )
        return cur.rowcount == 1

    def claim_event(self, key: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO processed_events (key, created_at) VALUES (?, ?)",
                (key, _encode("created_at", _now())),
            )
        return cur.rowcount == 1

    def release_event(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM processed_events WHERE key = ?", (key,))
