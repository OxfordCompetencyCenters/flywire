"""Top-level client — the single entry point for talking to the Flywire API."""

from __future__ import annotations

import httpx

from .config import FlywireConfig
from .http import HttpTransport
from .resources.checkout_sessions import CheckoutSessions
from .resources.payments import Payments


class FlywireClient:
    """Usage::

        with FlywireClient(FlywireConfig.from_env()) as flywire:
            session = flywire.checkout_sessions.create(request)
    """

    def __init__(self, config: FlywireConfig, *, http_client: httpx.Client | None = None) -> None:
        self.config = config
        self._transport = HttpTransport(config, client=http_client)
        self.checkout_sessions = CheckoutSessions(self._transport)
        self.payments = Payments(self._transport)

    @classmethod
    def from_env(cls) -> "FlywireClient":
        return cls(FlywireConfig.from_env())

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "FlywireClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"FlywireClient(environment={self.config.environment.value!r})"
