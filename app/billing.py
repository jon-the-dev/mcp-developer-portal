"""Stripe billing integration for call-based MCP platform tiers.

Tiers:
  - free: 1,000 calls/month (no Stripe subscription required)
  - pro: unlimited calls/month (flat rate Stripe subscription)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import stripe

from app.config import settings
from app import db

logger = logging.getLogger(__name__)


def _client() -> stripe.StripeClient:
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")
    return stripe.StripeClient(settings.stripe_secret_key)


def current_year_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def check_rate_limit(api_key: str) -> tuple[bool, int]:
    """Return (is_allowed, current_count).

    For free-tier keys, returns False when the monthly limit is reached.
    Pro-tier keys are always allowed.
    """
    key_record = db.get_api_key(api_key)
    if not key_record or not key_record.get("IsActive"):
        return False, 0

    tier = key_record.get("Tier", "free")
    if tier == "pro":
        return True, -1  # unlimited

    year_month = current_year_month()
    count = db.get_call_count(api_key, year_month)
    return count < settings.free_tier_monthly_calls, count


def record_call(api_key: str) -> int:
    """Increment call counter and return new count."""
    return db.increment_call_count(api_key, current_year_month())


def create_customer(user_id: str, email: str) -> str:
    client = _client()
    customer = client.customers.create(
        params={"email": email, "metadata": {"user_id": user_id}}
    )
    return customer.id


def create_pro_subscription(stripe_customer_id: str) -> dict:
    client = _client()
    if not settings.stripe_pro_price_id:
        raise ValueError("STRIPE_PRO_PRICE_ID not configured")
    subscription = client.subscriptions.create(
        params={
            "customer": stripe_customer_id,
            "items": [{"price": settings.stripe_pro_price_id}],
            "metadata": {"tier": "pro"},
        }
    )
    return {"subscription_id": subscription.id, "status": subscription.status}


def cancel_subscription(stripe_subscription_id: str) -> None:
    client = _client()
    client.subscriptions.update(
        stripe_subscription_id,
        params={"cancel_at_period_end": True},
    )


def construct_webhook_event(payload: bytes, sig_header: str) -> stripe.Event:
    if not settings.stripe_webhook_secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not configured")
    return stripe.Webhook.construct_event(
        payload, sig_header, settings.stripe_webhook_secret
    )
