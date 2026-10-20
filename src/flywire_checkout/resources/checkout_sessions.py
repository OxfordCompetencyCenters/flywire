"""``/checkout/sessions`` endpoints."""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from ..exceptions import (
    InvalidConfirmUrlError,
    NotFoundError,
    SessionAlreadyCompletedError,
    UnprocessableEntityError,
)
from ..http import HttpTransport
from ..models.sessions import (
    CheckoutSession,
    CreateCheckoutSessionRequest,
    ExpireOutcome,
    SessionConfirmation,
)

# Flywire documents IDs as UUIDs but reserves the right to change ID formats, so this
# is a conservative charset check rather than a UUID parse.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid checkout session id")
    return session_id


class CheckoutSessions:
    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, request: CreateCheckoutSessionRequest) -> CheckoutSession:
        response = self._transport.request("POST", "checkout/sessions", json=request.to_payload())
        return CheckoutSession.model_validate(response.json())

    def confirm(self, session_id: str) -> SessionConfirmation:
        """Confirm a submitted session. Can only succeed once per session."""
        path = f"checkout/sessions/{quote(validate_session_id(session_id), safe='')}/confirm"
        response = self._transport.request("POST", path)
        return SessionConfirmation.model_validate(response.json() if response.content else {})

    def expire(self, session_id: str) -> ExpireOutcome:
        """Cancel a session before its 30-minute timeout.

        Returns ``NOT_FOUND`` when there is nothing to expire (Flywire: "no action required").
        Raises :class:`SessionAlreadyCompletedError` when the payer already submitted the form —
        the right reaction is to confirm the session instead.
        """
        path = f"checkout/sessions/{quote(validate_session_id(session_id), safe='')}/expire"
        try:
            self._transport.request("POST", path)
        except NotFoundError:
            return ExpireOutcome.NOT_FOUND
        except UnprocessableEntityError as exc:
            # Only the documented "already completed" case; other 422s (e.g. malformed id) stay as-is.
            if any(e.get("type") == "unprocessable_entity" for e in exc.errors):
                raise SessionAlreadyCompletedError(
                    exc.status_code, exc.title, exc.detail, exc.errors, exc.request_id
                ) from exc
            raise
        return ExpireOutcome.EXPIRED

    def session_id_from_confirm_url(self, confirm_url: str) -> str:
        """Extract the session id from a postMessage ``confirm_url`` after strict validation.

        The confirm URL arrives from the browser, so it is untrusted. We never send our API key
        to it directly; instead we check that it points at exactly our configured API host and
        the documented path, then rebuild the request from the extracted session id.
        """
        if not isinstance(confirm_url, str) or len(confirm_url) > 2048:
            raise InvalidConfirmUrlError("confirm_url is missing or too long")
        expected = urlsplit(self._transport.base_url)
        actual = urlsplit(confirm_url)
        if (
            actual.scheme != "https"
            or actual.hostname != expected.hostname
            or actual.port not in (None, 443)
            or actual.username
            or actual.password
            or actual.query
            or actual.fragment
        ):
            raise InvalidConfirmUrlError("confirm_url does not point at the configured Flywire API")
        prefix = expected.path.rstrip("/") + "/checkout/sessions/"
        if not actual.path.startswith(prefix) or not actual.path.endswith("/confirm"):
            raise InvalidConfirmUrlError("confirm_url has an unexpected path")
        session_id = actual.path[len(prefix) : -len("/confirm")]
        try:
            return validate_session_id(session_id)
        except ValueError:
            raise InvalidConfirmUrlError("confirm_url contains an invalid session id") from None
