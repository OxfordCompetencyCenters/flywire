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
    report = sub.add_parser("report", help="export payments (who, how much, when, what) as CSV or JSON")
    report.add_argument("--site", help="one site id (default: all sites)")
    report.add_argument("--from", dest="start", help="YYYY-MM-DD (checkout created on/after, UTC)")
    report.add_argument("--to", dest="end", help="YYYY-MM-DD (inclusive)")
    report.add_argument("--status", help="comma-separated payment/checkout statuses, e.g. guaranteed,delivered")
    report.add_argument("--format", choices=["csv", "json"], default="csv")
    report.add_argument("--refresh", action="store_true", help="fetch missing details from Flywire first")
    report.add_argument("--output", help="file to write (created with owner-only permissions); default stdout")
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

    if args.command == "report":
        return _report(service, args)

    import uvicorn

    from .app import create_gateway_app

    uvicorn.run(create_gateway_app(service), host=args.host, port=args.port, proxy_headers=True)
    return 0


def _report(service, args) -> int:
    import json
    import os
    from datetime import date

    from .app import report_window
    from .reports import to_csv
    from .service import InvalidRequest

    site = None
    if args.site:
        site = service.sites.get(args.site)
        if site is None:
            print(f"Unknown site {args.site!r}", file=sys.stderr)
            return 2
    try:
        start = date.fromisoformat(args.start) if args.start else None
        end = date.fromisoformat(args.end) if args.end else None
        window = report_window(start, end)
    except (ValueError, InvalidRequest) as exc:
        print(f"Invalid dates: {exc}", file=sys.stderr)
        return 2
    statuses = {s.strip() for s in args.status.split(",") if s.strip()} if args.status else None
    rows, columns = service.payment_report(site, *window, statuses=statuses, refresh=args.refresh)
    text = to_csv(rows, columns) if args.format == "csv" else json.dumps(
        {"columns": columns, "rows": rows, "count": len(rows)}, indent=2, default=str)
    if args.output:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # contains personal data
        os.fchmod(fd, 0o600)  # also tighten a pre-existing file
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        print(f"Wrote {len(rows)} payment(s) to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
