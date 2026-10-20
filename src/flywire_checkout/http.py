"""HTTP transport: authentication, timeouts, retries, error mapping and safe logging.

This is the only module that touches the network or the API key.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from ._version import __version__
from .config import FlywireConfig
from .exceptions import FlywireAPIError, TransportError, error_class_for_status

logger = logging.getLogger("flywire_checkout.http")

_AUTH_HEADER = "X-Authentication-Key"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 429 is deliberately absent: Flywire blocks the IP for 10 minutes, so retrying only makes it worse.
_RETRYABLE_STATUS = frozenset({502, 503, 504})


class HttpTransport:
    def __init__(self, config: FlywireConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds),
            # Never follow redirects: that could forward the API key to another host.
            follow_redirects=False,
            verify=True,
        )

    @property
    def base_url(self) -> str:
        return self._config.api_base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {
            _AUTH_HEADER: self._config.api_key.get_secret_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"flywire-checkout-python/{__version__}",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send a request relative to the API base URL and return a 2xx response, or raise."""
        method = method.upper()
        if path.startswith(("/", "http:", "https:")) or ".." in path:
            raise ValueError("path must be relative to the API base URL")
        url = self.base_url + path
        attempts = 1 + (self._config.max_retries if method in _SAFE_METHODS else 0)

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = self._client.request(method, url, json=json, params=params, headers=self._headers())
            except httpx.TimeoutException as exc:
                if attempt < attempts:
                    self._backoff(attempt)
                    continue
                raise TransportError(f"Timed out calling Flywire {method} {path}") from exc
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    self._backoff(attempt)
                    continue
                raise TransportError(f"Network error calling Flywire {method} {path}: {type(exc).__name__}") from exc

            elapsed_ms = (time.monotonic() - started) * 1000
            # Log metadata only — bodies contain payer PII and tokens.
            logger.info(
                "flywire %s %s -> %s (%.0f ms, attempt %d/%d)",
                method, path, response.status_code, elapsed_ms, attempt, attempts,
            )

            if response.is_success:
                return response
            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                self._backoff(attempt)
                continue
            raise self._to_error(response)

        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.25))

    @staticmethod
    def _to_error(response: httpx.Response) -> FlywireAPIError:
        title = detail = None
        errors: list[dict[str, Any]] = []
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            title = body.get("title") if isinstance(body.get("title"), str) else None
            detail = body.get("detail") if isinstance(body.get("detail"), str) else None
            raw_errors = body.get("errors")
            if isinstance(raw_errors, list):
                errors = [e for e in raw_errors if isinstance(e, dict)]
        error_cls = error_class_for_status(response.status_code)
        return error_cls(
            status_code=response.status_code,
            title=title or response.reason_phrase,
            detail=detail,
            errors=errors,
            request_id=response.headers.get("x-request-id"),
        )
