"""Multi-site payment gateway: one Flywire integration, many websites.

Websites are declared in ``sites.toml``; secrets come from environment variables.
Build the ASGI app with :func:`build_gateway_app` (``python -m flywire_checkout.gateway serve``).
"""

from __future__ import annotations

from collections.abc import Mapping

from ..client import FlywireClient
from ..config import FlywireConfig
from ..webhooks import WebhookVerifier
from .app import create_gateway_app
from .events import EventForwarder, verify as verify_event_signature
from .service import CreateCheckoutInput, GatewayService
from .settings import GatewaySettings
from .sites import SiteConfig, SiteRegistry
from .store import Checkout, CheckoutStatus, GatewayStore, SqliteGatewayStore


def build_gateway_service(environ: Mapping[str, str] | None = None) -> GatewayService:
    settings = GatewaySettings.from_env(environ)
    flywire = FlywireConfig.from_env(environ)
    return GatewayService(
        settings=settings,
        sites=SiteRegistry.load(settings.sites_file, environ),
        client=FlywireClient(flywire),
        store=SqliteGatewayStore(settings.db_path),
        forwarder=EventForwarder(),
        verifier=WebhookVerifier(flywire.shared_secret) if flywire.shared_secret else None,
    )


def build_gateway_app(environ: Mapping[str, str] | None = None):
    return create_gateway_app(build_gateway_service(environ))


__all__ = [
    "Checkout",
    "CheckoutStatus",
    "CreateCheckoutInput",
    "EventForwarder",
    "GatewayService",
    "GatewaySettings",
    "GatewayStore",
    "SiteConfig",
    "SiteRegistry",
    "SqliteGatewayStore",
    "build_gateway_app",
    "build_gateway_service",
    "create_gateway_app",
    "verify_event_signature",
]
