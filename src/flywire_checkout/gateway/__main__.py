"""Gateway command line.

    python -m flywire_checkout.gateway serve [--host 0.0.0.0] [--port 8080]
    python -m flywire_checkout.gateway check-config
    python -m flywire_checkout.gateway new-key
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flywire_checkout.gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the gateway HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("check-config", help="validate settings and sites.toml, then exit")
    sub.add_parser("new-key", help="print a new random key for a site or GATEWAY_SECRET")
    args = parser.parse_args(argv)

    if args.command == "new-key":
        print(secrets.token_urlsafe(32))
        return 0

    _load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from ..exceptions import ConfigurationError
    from . import build_gateway_service

    try:
        service = build_gateway_service()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "check-config":
        cfg = service.client.config
        print(f"Flywire environment : {cfg.environment.value} ({cfg.api_base_url})")
        print(f"Gateway public URL  : {service.settings.public_url}")
        print(f"Webhook verification: {'on' if service.verifier else 'OFF (no shared secret)'}")
        for site in service.sites:
            print(f"  - {site.id:20} {site.name!r:30} recipient={site.recipient_id or cfg.recipient_id} "
                  f"origins={','.join(site.origins)} webhooks={'yes' if site.webhook_url else 'no'}")
        return 0

    import uvicorn

    from .app import create_gateway_app

    uvicorn.run(create_gateway_app(service), host=args.host, port=args.port, proxy_headers=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
