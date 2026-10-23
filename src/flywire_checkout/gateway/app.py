"""HTTP surface of the payment gateway.

Site API (server-to-server, ``Authorization: Bearer <site api key>``)::

    POST /v1/checkouts                  create (idempotent per order_reference)
    GET  /v1/checkouts/{id}             status
    POST /v1/checkouts/{id}/cancel      cancel before the payer submits

Payer (browser, authorised by the unguessable payment link)::

    GET  /pay/{id}/{token}              payment page (redirect to it, or embed it in an iframe)
    GET  /pay/{id}/{token}/session      page bootstrap data
    POST /pay/{id}/{token}/confirm      confirm after the Flywire form was submitted
    POST /pay/{id}/{token}/client-error report SDK/iframe errors

Flywire::

    POST /webhooks/flywire              payment status notifications (X-Flywire-Digest)
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..webhooks import DIGEST_HEADER, MAX_BODY_BYTES
from .reports import to_csv
from .service import CreateCheckoutInput, GatewayError, GatewayService, InvalidRequest, public_view
from .sites import SiteConfig
from .store import CheckoutStatus

logger = logging.getLogger("flywire_checkout.gateway.http")

_STATIC = Path(__file__).parent / "static"
_STATIC_FILES = {"pay.js": "text/javascript", "embed.js": "text/javascript"}


_PAY_LINK = re.compile(r"(/pay/chk_[A-Za-z0-9_-]{24}/)[A-Za-z0-9_-]+")


class RedactPaymentLinks(logging.Filter):
    """Payment-link tokens act like bearer credentials: keep them out of access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(_PAY_LINK.sub(r"\1<redacted>", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _PAY_LINK.sub(r"\1<redacted>", record.msg)
        return True


def _install_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "httpx"):
        target = logging.getLogger(name)
        if not any(isinstance(f, RedactPaymentLinks) for f in target.filters):
            target.addFilter(RedactPaymentLinks())


def report_window(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    """Inclusive UTC date range -> [from, to) datetimes."""
    if start and end and end < start:
        raise InvalidRequest("'to' must not be before 'from'")
    created_from = datetime.combine(start, time.min, tzinfo=timezone.utc) if start else None
    created_to = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc) if end else None
    return created_from, created_to


def _asset_version(name: str) -> str:
    """Content hash for cache-busting, so a deploy takes effect immediately in browsers."""
    return hashlib.sha256((_STATIC / name).read_bytes()).hexdigest()[:12]


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_url: str = Field(min_length=1, max_length=2048)


class ClientErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: str | dict | list | None = None


def _with_query(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urlunsplit(parts._replace(query=urlencode(query)))


def _authenticated_site(request: Request) -> SiteConfig:
    """Site API auth. A dependency, so it runs before body validation: unauthenticated
    callers never see validation details."""
    service: GatewayService = request.app.state.gateway
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    site = service.sites.authenticate(key.strip()) if scheme.lower() == "bearer" else None
    if site is None:
        raise HTTPException(401, "Invalid or missing site API key", headers={"WWW-Authenticate": "Bearer"})
    return site


AuthenticatedSite = Annotated[SiteConfig, Depends(_authenticated_site)]


def create_gateway_app(service: GatewayService) -> FastAPI:
    app = FastAPI(title="Flywire Payment Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = service
    _install_log_redaction()
    pay_js_version = _asset_version("pay.js")
    flywire = service.client.config

    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(
            {"error": {"code": codes.get(exc.status_code, "http_error"), "message": str(exc.detail)}},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "Request validation failed", "details": details}},
            status_code=422,
        )

    @app.middleware("http")
    async def _baseline_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ------------------------------------------------------------------ site API

    @app.post("/v1/checkouts")
    def create_checkout(site: AuthenticatedSite, body: CreateCheckoutInput) -> JSONResponse:
        checkout, created = service.create_checkout(site, body)
        payload = {**public_view(checkout), "payment_url": service.payment_url(checkout.id)}
        return JSONResponse(payload, status_code=201 if created else 200)

    @app.get("/v1/checkouts/{checkout_id}")
    def get_checkout(checkout_id: str, site: AuthenticatedSite) -> dict:
        checkout = service.get_checkout(site, checkout_id)
        return {**public_view(checkout), "payment_url": service.payment_url(checkout.id)}

    @app.post("/v1/checkouts/{checkout_id}/cancel")
    def cancel_checkout(checkout_id: str, site: AuthenticatedSite) -> dict:
        return public_view(service.cancel_checkout(site, checkout_id))

    @app.get("/v1/reports/payments")
    def payments_report(
        site: AuthenticatedSite,
        start: Annotated[date | None, Query(alias="from")] = None,
        end: Annotated[date | None, Query(alias="to")] = None,
        status: str | None = None,
        format: Literal["csv", "json"] = "csv",  # noqa: A002 (public query parameter name)
        refresh: bool = False,
    ) -> Response:
        """Who paid, how much, when, for what — this site's checkouts only (contains personal data)."""
        created_from, created_to = report_window(start, end)
        statuses = {s.strip() for s in status.split(",") if s.strip()} if status else None
        rows, columns = service.payment_report(site, created_from, created_to, statuses=statuses, refresh=refresh)
        logger.info("report: site=%s rows=%d format=%s", site.id, len(rows), format)
        if format == "json":
            return JSONResponse({"columns": columns, "rows": rows, "count": len(rows)})
        name = f"payments_{site.id}_{start or 'all'}_{end or 'all'}.csv"
        return Response(to_csv(rows, columns), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ payer pages

    @app.get("/pay/{checkout_id}/{token}", response_class=HTMLResponse, include_in_schema=False)
    def payment_page(checkout_id: str, token: str) -> HTMLResponse:
        checkout, site = service.resolve_link(checkout_id, token)
        template = (_STATIC / "pay.html").read_text(encoding="utf-8")
        body = (
            template.replace("__PAY_JS_VERSION__", pay_js_version)
            .replace("__SDK_URL__", html.escape(flywire.profile.sdk_script_url, quote=True))
            .replace("__SITE_NAME__", html.escape(site.name))
            .replace("__CONFIG__", html.escape(json.dumps(
                {"base": f"/pay/{checkout.id}/{token}", "parent_origins": site.origins,
                 "cancel_url": _with_query(site.effective_cancel_url, {"checkout_id": checkout.id,
                                                                       "order_reference": checkout.order_reference})}),
                quote=True))
        )
        ancestors = " ".join(["'self'", *site.origins])
        csp = "; ".join([
            "default-src 'self'",
            f"script-src 'self' {flywire.profile.sdk_script_url} https://*.flywire.com",
            "frame-src https://*.flywire.com",
            "connect-src 'self' https://*.flywire.com",
            "img-src 'self' data: https://*.flywire.com",
            "style-src 'self' 'unsafe-inline' https://*.flywire.com https://fonts.googleapis.com",
            "font-src 'self' data: https://*.flywire.com https://fonts.gstatic.com",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            f"frame-ancestors {ancestors}",
        ])
        return HTMLResponse(body, headers={"Content-Security-Policy": csp})

    @app.get("/pay/{checkout_id}/{token}/session")
    def payment_session(checkout_id: str, token: str) -> dict:
        checkout, site = service.open_payment(checkout_id, token)
        done = checkout.status is CheckoutStatus.CONFIRMED
        return {
            "status": checkout.status.value,
            "site_name": site.name,
            "description": checkout.description,
            "amount": checkout.amount,
            "sdk_url": flywire.profile.sdk_script_url,
            "frontend_key": flywire.frontend_key.get_secret_value() if flywire.frontend_key else None,
            "session_id": None if done else checkout.flywire_session_id,
            "hosted_form_url": None if done else checkout.hosted_form_url,
            "expires_at": checkout.flywire_session_expires_at.isoformat() if checkout.flywire_session_expires_at else None,
            "return_url": _with_query(site.return_url, {"checkout_id": checkout.id, "order_reference": checkout.order_reference}),
            "cancel_url": _with_query(site.effective_cancel_url, {"checkout_id": checkout.id, "order_reference": checkout.order_reference}),
            "parent_origins": site.origins,
            "environment": flywire.environment.value,
        }

    @app.post("/pay/{checkout_id}/{token}/confirm")
    def confirm_payment(checkout_id: str, token: str, body: ConfirmBody) -> dict:
        checkout = service.confirm_payment(checkout_id, token, body.confirm_url)
        return {"status": checkout.status.value, "payment_reference": checkout.payment_reference}

    @app.post("/pay/{checkout_id}/{token}/client-error", status_code=204)
    def client_error(checkout_id: str, token: str, body: ClientErrorBody) -> Response:
        service.record_client_error(checkout_id, token, body.error)
        return Response(status_code=204)

    @app.get("/static/{name}", include_in_schema=False)
    def static_file(name: str) -> Response:
        if name not in _STATIC_FILES:
            raise HTTPException(404)
        return Response(
            (_STATIC / name).read_bytes(),
            media_type=_STATIC_FILES[name],
            headers={"Cache-Control": "public, max-age=300", "Cross-Origin-Resource-Policy": "cross-origin"},
        )

    # ------------------------------------------------------------------ Flywire + ops

    @app.post("/webhooks/flywire", include_in_schema=False)
    async def flywire_webhook(request: Request) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return Response(status_code=413)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return Response(status_code=413)
            chunks.append(chunk)
        status = await run_in_threadpool(
            service.handle_flywire_notification, b"".join(chunks), request.headers.get(DIGEST_HEADER)
        )
        return Response(status_code=status)

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok", "environment": flywire.environment.value, "sites": len(service.sites)}

    return app
