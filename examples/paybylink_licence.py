"""Reference: licence purchase with Flywire Pay-By-Link (the flow proposed for Halo).

    ticket approved ──► POST /tickets/{id}/payment-link ──► JSON Link (single use, fixed amount)
    payer pays on Flywire's hosted portal
    Flywire ──► POST /flywire/callback (X-Flywire-Digest) ──► verify ─► de-duplicate ─► decide ─► grant licence

Run locally (needs FastAPI):
    FLYWIRE_PORTAL_SHARED_SECRET=... FLYWIRE_PORTAL_SUBDOMAIN=oxfordtesting \
    uvicorn examples.paybylink_licence:app --port 8002

Halo (or any platform) implements the same three steps in its own runbook/integration layer;
the Node equivalents are in clients/node/flywire-paybylink.mjs.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Response

from flywire_checkout import Environment, SignatureVerificationError, WebhookVerifier
from flywire_checkout.paybylink import (
    Decision,
    Expectation,
    FulfilmentPolicy,
    JsonLinkRequest,
    PayByLinkClient,
)
from flywire_checkout.webhooks import DIGEST_HEADER, MAX_BODY_BYTES

logger = logging.getLogger("licence")

# Fixed by configuration — never taken from the user or the URL.
LICENCE_PRICE = 12500  # £125.00 in pence
LICENCE_CURRENCY = "GBP"


@dataclass
class Ticket:
    id: str
    first_name: str
    last_name: str
    email: str
    payment_link: str | None = None
    payment_reference: str | None = None
    payment_status: str | None = None
    status_at: datetime | None = None
    licence_granted: bool = False
    needs_review: str | None = None
    seen_events: set[str] = field(default_factory=set)


class TicketStore:
    """Stand-in for Halo tickets. Use your platform's storage with the same rules."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tickets: dict[str, Ticket] = {}


def grant_licence(ticket: Ticket) -> None:  # e.g. add the user to the licence group
    logger.info("GRANT licence for ticket %s (%s)", ticket.id, ticket.email)


def revoke_licence(ticket: Ticket) -> None:
    logger.info("REVOKE licence for ticket %s", ticket.id)


def create_app(link_client: PayByLinkClient, shared_secret: str, subdomain: str, store: TicketStore) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    verifier = WebhookVerifier(shared_secret)
    policy = FulfilmentPolicy()

    @app.post("/tickets/{ticket_id}/payment-link")
    def payment_link(ticket_id: str) -> dict:
        ticket = store.tickets.get(ticket_id)
        if ticket is None:
            raise HTTPException(404, "unknown ticket")
        if ticket.licence_granted:
            raise HTTPException(409, "already paid")
        if ticket.payment_link is None:  # one link per ticket; it expires after one payment
            link = link_client.create_link(JsonLinkRequest(
                payment_destination=subdomain,
                amount=LICENCE_PRICE,
                callback_id=ticket.id,
                dynamic_fields={"student_first_name": ticket.first_name, "student_last_name": ticket.last_name,
                                "student_email": ticket.email},
                sender_email=ticket.email,
                days_to_expire=7,
            ))
            ticket.payment_link = link.url
        return {"payment_link": ticket.payment_link}

    @app.post("/flywire/callback")
    async def flywire_callback(request: Request) -> Response:
        raw = await request.body()  # the exact bytes Flywire signed
        if len(raw) > MAX_BODY_BYTES:
            return Response(status_code=413)
        try:
            notification = verifier.parse_payment_notification(raw, request.headers.get(DIGEST_HEADER))
        except SignatureVerificationError:
            return Response(status_code=401)
        except ValueError:
            return Response(status_code=202)  # signed but unreadable — retrying won't help

        with store.lock:
            ticket = store.tickets.get(notification.data.external_reference or "")
            if ticket is None:
                logger.warning("callback for unknown reference %r", notification.data.external_reference)
                return Response(status_code=200)
            key = notification.idempotency_key
            if key in ticket.seen_events:  # Flywire retries up to 5 more times
                return Response(status_code=200)
            ticket.seen_events.add(key)
            if ticket.status_at and notification.event_date < ticket.status_at:  # stale retry
                return Response(status_code=200)
            ticket.payment_status = notification.data.status or notification.event_type
            ticket.status_at = notification.event_date
            ticket.payment_reference = notification.data.payment_id

            result = policy.evaluate(notification, Expectation(ticket.id, LICENCE_PRICE, LICENCE_CURRENCY))
            logger.info("ticket %s: %s (%s)", ticket.id, result.decision.value, result.reason)
            if result.decision is Decision.GRANT and not ticket.licence_granted:
                grant_licence(ticket)
                ticket.licence_granted = True
            elif result.decision is Decision.REVOKE and ticket.licence_granted:
                revoke_licence(ticket)
                ticket.licence_granted = False
            elif result.decision is Decision.MISMATCH:
                ticket.needs_review = result.reason  # paid but not as expected: a human decides
        return Response(status_code=200)

    return app


if os.environ.get("FLYWIRE_PORTAL_SHARED_SECRET"):
    _secret = os.environ["FLYWIRE_PORTAL_SHARED_SECRET"]
    app = create_app(PayByLinkClient(_secret, Environment.parse(os.environ.get("FLYWIRE_ENVIRONMENT", "sandbox"))),
                     _secret, os.environ.get("FLYWIRE_PORTAL_SUBDOMAIN", "oxfordtesting"), TicketStore())
