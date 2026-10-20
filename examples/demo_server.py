"""Local sandbox demo.

    cp .env.example .env        # fill in FLYWIRE_SANDBOX_* values
    pip install -e ".[web]"
    python examples/demo_server.py
    open http://127.0.0.1:8000/checkout

Replace ``demo_owner`` and ``resolve_order`` with your platform's auth and order lookup.
"""

from __future__ import annotations

import logging

import uvicorn
from dotenv import load_dotenv
from fastapi import Request

from flywire_checkout import FlywireClient, FlywireConfig, WebhookVerifier
from flywire_checkout.models import CreateCheckoutSessionRequest, Payor, Recipient
from flywire_checkout.models.notifications import PaymentNotification
from flywire_checkout.services import CheckoutService, InMemoryCheckoutStore, NotificationProcessor
from flywire_checkout.web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

load_dotenv()
config = FlywireConfig.from_env()
if config.is_production:
    raise SystemExit("The demo server uses a fake login and in-memory store; refusing to run against production.")
if not config.recipient_id:
    raise SystemExit("Set FLYWIRE_SANDBOX_RECIPIENT_ID in .env")

# Server-side price list: the browser only sends an order id, never an amount.
ORDERS = {"demo-order-1": 33000, "demo-order-2": 12025}  # subunits (330.00, 120.25)


def demo_owner(request: Request) -> str:
    return "demo-user"  # replace with your authenticated user id


def resolve_order(owner_id: str, order_id: str) -> CreateCheckoutSessionRequest:
    amount = ORDERS.get(order_id)
    if amount is None:
        raise LookupError(order_id)
    return CreateCheckoutSessionRequest.one_off(
        amount=amount,
        recipient_id=config.recipient_id,
        external_reference=f"{owner_id}:{order_id}"[:50],
        payor_id=owner_id,
        payor=Payor(country="GB"),
        # Portal-specific required fields (D0226 requires these). Take them from your student record.
        recipient=Recipient.from_values(
            {"student_first_name": "Demo", "student_last_name": "Student", "student_email": "demo.student@example.com"}
        ),
    )


store = InMemoryCheckoutStore()
client = FlywireClient(config)
service = CheckoutService(client, store)
processor = NotificationProcessor(WebhookVerifier(config.shared_secret), store) if config.shared_secret else None

if processor:

    @processor.on("guaranteed")
    def fulfil(notification: PaymentNotification) -> None:
        # Use the guaranteed amount_to, not the initiated one: bank transfers can be slightly
        # under/over-paid within Flywire's tolerance.
        logging.info(
            "payment %s guaranteed (%s %s) — fulfil order %s",
            notification.data.payment_id, notification.data.amount_to, notification.data.currency_to,
            notification.data.external_reference,
        )


app = create_app(
    config=config,
    service=service,
    processor=processor,
    owner_id_provider=demo_owner,
    order_resolver=resolve_order,
)

if __name__ == "__main__":
    logging.info("Flywire environment: %s (%s)", config.environment.value, config.api_base_url)
    uvicorn.run(app, host="127.0.0.1", port=8000)
