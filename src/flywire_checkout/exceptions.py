"""Exception hierarchy.

Callers can catch :class:`FlywireError` for everything, or narrower classes to
react to specific situations (e.g. :class:`RateLimitError` → back off for 10 minutes).
"""

from __future__ import annotations

from typing import Any


class FlywireError(Exception):
    """Base class for all errors raised by this package."""


class ConfigurationError(FlywireError):
    """Missing or invalid configuration (credentials, URLs, environment)."""


class TransportError(FlywireError):
    """Network-level failure (DNS, TLS, timeout, connection reset) — no HTTP response received."""


class FlywireAPIError(FlywireError):
    """Flywire returned a non-success HTTP status.

    Flywire errors follow a problem-details shape::

        {"type": ..., "title": ..., "status": 422, "detail": ..., "errors": [{"param": ..., "message": ...}]}
    """

    def __init__(
        self,
        status_code: int,
        title: str | None = None,
        detail: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.errors = errors or []
        self.request_id = request_id
        super().__init__(self._render())

    def _render(self) -> str:
        parts = [f"HTTP {self.status_code}"]
        if self.title:
            parts.append(self.title)
        if self.detail:
            parts.append(self.detail)
        messages = [
            f"{e['param']}: {e['message']}" if e.get("param") else e["message"]
            for e in self.errors
            if isinstance(e, dict) and e.get("message")
        ]
        if messages:
            parts.append("; ".join(messages))
        return " - ".join(parts)


class BadRequestError(FlywireAPIError):
    """400."""


class AuthenticationError(FlywireAPIError):
    """401/403 — wrong API key, or a key for the other environment."""


class NotFoundError(FlywireAPIError):
    """404."""


class UnprocessableEntityError(FlywireAPIError):
    """422 — validation or state error (e.g. session already confirmed)."""


class SessionAlreadyCompletedError(UnprocessableEntityError):
    """Expire was called on a session the payer already submitted — confirm it instead."""


class RateLimitError(FlywireAPIError):
    """429 — Flywire blocks the caller IP for 10 minutes after exceeding 500 req/min."""


class ServerError(FlywireAPIError):
    """5xx."""


class InvalidConfirmUrlError(FlywireError):
    """The confirm URL received from the browser does not point at the configured Flywire API."""


class SignatureVerificationError(FlywireError):
    """A webhook's X-Flywire-Digest header is missing or does not match the payload."""


class CheckoutStateError(FlywireError):
    """Application-level state violation (unknown session, wrong owner, double confirm)."""


def error_class_for_status(status_code: int) -> type[FlywireAPIError]:
    if status_code == 400:
        return BadRequestError
    if status_code in (401, 403):
        return AuthenticationError
    if status_code == 404:
        return NotFoundError
    if status_code == 422:
        return UnprocessableEntityError
    if status_code == 429:
        return RateLimitError
    if status_code >= 500:
        return ServerError
    return FlywireAPIError
