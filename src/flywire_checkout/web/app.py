"""FastAPI adapter (optional, ``pip install .[web]``).

Thin HTTP layer over :class:`CheckoutService` and :class:`NotificationProcessor`.
Your platform plugs in two things:

* ``owner_id_provider`` — returns the authenticated user's id for a request
  (raise ``HTTPException(401)`` if unauthenticated). Sessions are bound to it.
* ``order_resolver`` — turns a client-supplied ``order_id`` into a server-trusted
  :class:`CreateCheckoutSessionRequest`. The browser never chooses the amount.
"""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..config import FlywireConfig
from ..exceptions import (
    CheckoutStateError,
    FlywireAPIError,
    FlywireError,
    InvalidConfirmUrlError,
    RateLimitError,
    SessionAlreadyCompletedError,
)
from ..models.sessions import CreateCheckoutSessionRequest
from ..services.checkout_service import CheckoutService
from ..services.notifications import NotificationProcessor
from ..webhooks import DIGEST_HEADER, MAX_BODY_BYTES

logger = logging.getLogger("flywire_checkout.web")

OwnerIdProvider = Callable[[Request], str]
OrderResolver = Callable[[str, str], CreateCheckoutSessionRequest]  # (owner_id, order_id) -> request

_STATIC = Path(__file__).parent / "static"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionBody(_Body):
    order_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ConfirmSessionBody(_Body):
    confirm_url: str = Field(min_length=1, max_length=2048)


class ClientErrorBody(_Body):
    error: str | dict | None = None


def create_router(
    *,
    config: FlywireConfig,
    service: CheckoutService,
    processor: NotificationProcessor | None,
    owner_id_provider: OwnerIdProvider,
    order_resolver: OrderResolver,
    api_prefix: str = "/api/flywire",
    webhook_path: str = "/webhooks/flywire",
) -> APIRouter:
    router = APIRouter()

    @router.post(f"{api_prefix}/checkout-sessions")
    def create_session(body: CreateSessionBody, request: Request) -> dict:
        owner_id = owner_id_provider(request)
        try:
            session_request = order_resolver(owner_id, body.order_id)
        except LookupError:
            raise HTTPException(404, "Order not found") from None
        with _translate_errors():
            session = service.create_session(owner_id, session_request)
        # Return only what the browser needs. Warnings stay server-side (logged).
        return {
            "session_id": session.id,
            "expires_in_seconds": session.expires_in_seconds,
            "hosted_form_url": session.hosted_form.url if session.hosted_form else None,
        }

    @router.post(f"{api_prefix}/checkout-sessions/confirm")
    def confirm_session(body: ConfirmSessionBody, request: Request) -> dict:
        owner_id = owner_id_provider(request)
        with _translate_errors():
            confirmation = service.confirm_session(owner_id, body.confirm_url)
        # Note: confirmation != payment success. Final status arrives via webhooks.
        return {
            "status": "confirmed",
            "payment_reference": confirmation.payment_reference,
            "payment_method_saved": bool(confirmation.payment_method and confirmation.payment_method.token),
        }

    @router.post(f"{api_prefix}/checkout-sessions/{{session_id}}/expire")
    def expire_session(session_id: str, request: Request) -> dict:
        owner_id = owner_id_provider(request)
        with _translate_errors():
            outcome = service.expire_session(owner_id, session_id)
        return {"status": outcome.value}

    @router.post(f"{api_prefix}/checkout-sessions/{{session_id}}/client-errors", status_code=204)
    def client_error(session_id: str, body: ClientErrorBody, request: Request) -> Response:
        owner_id = owner_id_provider(request)
        with _translate_errors():
            service.record_client_error(owner_id, session_id, body.error)
        return Response(status_code=204)

    if processor is not None:

        @router.post(webhook_path, include_in_schema=False)
        async def flywire_webhook(request: Request) -> Response:
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                return Response(status_code=413)
            # Read the exact raw bytes (required for the digest) with a hard cap, so chunked
            # bodies without Content-Length can't exhaust memory either.
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return Response(status_code=413)
                chunks.append(chunk)
            # Handlers typically do blocking DB work — keep it off the event loop.
            result = await run_in_threadpool(processor.process, b"".join(chunks), request.headers.get(DIGEST_HEADER))
            return Response(status_code=result.http_status)

    @router.get("/checkout", response_class=HTMLResponse, include_in_schema=False)
    def checkout_page() -> HTMLResponse:
        page_config = {
            "sdkUrl": config.profile.sdk_script_url,
            "frontendKey": config.frontend_key.get_secret_value() if config.frontend_key else None,
            "apiPrefix": api_prefix,
            "environment": config.environment.value,
        }
        template = (_STATIC / "checkout.html").read_text(encoding="utf-8")
        # Values are HTML-escaped so nothing can break out of the attribute they sit in.
        body = template.replace("__FLYWIRE_SDK_URL__", html.escape(config.profile.sdk_script_url, quote=True))
        body = body.replace("__FLYWIRE_CONFIG__", html.escape(json.dumps(page_config), quote=True))
        return HTMLResponse(body, headers=_page_security_headers(config.profile.sdk_script_url))

    @router.get("/checkout.js", include_in_schema=False)
    def checkout_script() -> Response:
        return Response((_STATIC / "checkout.js").read_bytes(), media_type="text/javascript")

    return router


def create_app(**router_kwargs) -> FastAPI:
    app = FastAPI(title="Flywire Checkout", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_router(**router_kwargs))

    @app.middleware("http")
    async def _baseline_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app


class _translate_errors:
    """Map domain errors to safe HTTP responses — never leak upstream details to the browser."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None or isinstance(exc, HTTPException):
            return False
        if isinstance(exc, InvalidConfirmUrlError):
            raise HTTPException(400, "Invalid confirmation request") from exc
        if isinstance(exc, CheckoutStateError):
            raise HTTPException(409, str(exc)) from exc
        if isinstance(exc, SessionAlreadyCompletedError):
            raise HTTPException(409, "Payer already submitted the form; confirm the session instead") from exc
        if isinstance(exc, RateLimitError):
            raise HTTPException(503, "Payment provider temporarily unavailable") from exc
        if isinstance(exc, FlywireAPIError) and exc.status_code == 422:
            logger.warning("flywire rejected request: %s", exc)
            raise HTTPException(422, "Payment request was rejected by the provider") from exc
        if isinstance(exc, FlywireError):
            logger.error("flywire call failed: %s", exc)
            raise HTTPException(502, "Payment provider error") from exc
        if isinstance(exc, ValueError):
            raise HTTPException(400, "Invalid request") from exc
        return False


def _page_security_headers(sdk_url: str) -> dict[str, str]:
    csp = "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' {sdk_url} https://*.flywire.com",
            "frame-src https://*.flywire.com",
            "connect-src 'self' https://*.flywire.com",
            "img-src 'self' data: https://*.flywire.com",
            # The Flywire SDK pulls Google Fonts.
            "style-src 'self' 'unsafe-inline' https://*.flywire.com https://fonts.googleapis.com",
            "font-src 'self' data: https://*.flywire.com https://fonts.gstatic.com",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ]
    )
    return {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"}
