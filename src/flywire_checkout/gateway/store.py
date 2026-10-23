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

    # --- reporting --------------------------------------------------------------------

    def save_payment_details(self, checkout_id: str, payment_reference: str, details: dict[str, Any]) -> None:
        """Store/merge the latest Flywire payment snapshot for a checkout."""
        ...

    def get_payment_details(self, checkout_ids: list[str]) -> dict[str, dict[str, Any]]: ...

    def add_payment_event(self, checkout_id: str, key: str, event_type: str, status: str | None,
                          event_date: datetime) -> None:
        """Append to the payment's status history (idempotent on key)."""
        ...

    def get_payment_events(self, checkout_ids: list[str]) -> dict[str, list[dict[str, Any]]]: ...

    def list_checkouts(self, site_id: str | None, created_from: datetime | None, created_to: datetime | None,
                       limit: int) -> list[Checkout]:
        """Newest first. ``site_id`` None = all sites."""
        ...


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
CREATE INDEX IF NOT EXISTS checkouts_site_created ON checkouts (site_id, created_at);
-- Reporting tables (added Oct 2026; created automatically on existing databases).
CREATE TABLE IF NOT EXISTS payment_details (
    checkout_id TEXT PRIMARY KEY REFERENCES checkouts (id),
    payment_reference TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_events (
    key TEXT PRIMARY KEY,
    checkout_id TEXT NOT NULL REFERENCES checkouts (id),
    event_type TEXT NOT NULL,
    status TEXT,
    event_date TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS payment_events_checkout ON payment_events (checkout_id, event_date);
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

    # --- reporting --------------------------------------------------------------------

    def save_payment_details(self, checkout_id: str, payment_reference: str, details: dict[str, Any]) -> None:
        with self._lock:
            row = self._db.execute("SELECT data FROM payment_details WHERE checkout_id = ?", (checkout_id,)).fetchone()
            merged = {**(json.loads(row["data"]) if row else {}), **{k: v for k, v in details.items() if v is not None}}
            self._db.execute(
                "INSERT INTO payment_details (checkout_id, payment_reference, data, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (checkout_id) DO UPDATE SET payment_reference = excluded.payment_reference, "
                "data = excluded.data, updated_at = excluded.updated_at",
                (checkout_id, payment_reference, json.dumps(merged, default=str), _encode("updated_at", _now())),
            )

    def get_payment_details(self, checkout_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not checkout_ids:
            return {}
        marks = ", ".join("?" for _ in checkout_ids)
        with self._lock:
            rows = self._db.execute(
                f"SELECT checkout_id, data FROM payment_details WHERE checkout_id IN ({marks})",  # noqa: S608
                checkout_ids,
            ).fetchall()
        return {r["checkout_id"]: json.loads(r["data"]) for r in rows}

    def add_payment_event(self, checkout_id: str, key: str, event_type: str, status: str | None,
                          event_date: datetime) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO payment_events (key, checkout_id, event_type, status, event_date, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, checkout_id, event_type, status, _encode("created_at", event_date), _encode("created_at", _now())),
            )

    def get_payment_events(self, checkout_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not checkout_ids:
            return {}
        marks = ", ".join("?" for _ in checkout_ids)
        with self._lock:
            rows = self._db.execute(
                f"SELECT checkout_id, event_type, status, event_date FROM payment_events "  # noqa: S608
                f"WHERE checkout_id IN ({marks}) ORDER BY event_date",
                checkout_ids,
            ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["checkout_id"], []).append(
                {"event_type": r["event_type"], "status": r["status"], "event_date": r["event_date"]}
            )
        return out

    def list_checkouts(self, site_id: str | None, created_from: datetime | None, created_to: datetime | None,
                       limit: int) -> list[Checkout]:
        clauses, params = [], []
        if site_id is not None:
            clauses.append("site_id = ?")
            params.append(site_id)
        if created_from is not None:
            clauses.append("created_at >= ?")
            params.append(_encode("created_at", created_from))
        if created_to is not None:
            clauses.append("created_at < ?")
            params.append(_encode("created_at", created_to))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM checkouts {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608 (fixed clauses)
                [*params, limit],
            ).fetchall()
        return [_decode(r) for r in rows]
