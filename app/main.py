"""MCP Developer Portal — FastAPI application."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi import Limiter
from slowapi.util import get_remote_address
import secrets

from app.config import settings
from app import billing, db

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Gateway middleware endpoint — called by MCP gateway on every request
# ---------------------------------------------------------------------------


class GatewayCheckRequest(BaseModel):
    api_key: str


class GatewayCheckResponse(BaseModel):
    allowed: bool
    tier: str
    calls_this_month: int
    limit: int | None = None  # None = unlimited


@app.post("/v1/gateway/check", response_model=GatewayCheckResponse)
async def gateway_check(body: GatewayCheckRequest) -> GatewayCheckResponse:
    """Called by the MCP gateway before forwarding a request.

    Returns 200 with allowed=True/False; gateway should return 429 when False.
    """
    allowed, count = billing.check_rate_limit(body.api_key)
    key_record = db.get_api_key(body.api_key) or {}
    tier = key_record.get("Tier", "free")

    if allowed and tier == "free":
        # Record the call only if allowed (do not double-count)
        count = billing.record_call(body.api_key)

    return GatewayCheckResponse(
        allowed=allowed,
        tier=tier,
        calls_this_month=count if count >= 0 else 0,
        limit=settings.free_tier_monthly_calls if tier == "free" else None,
    )


# ---------------------------------------------------------------------------
# API Key management
# ---------------------------------------------------------------------------


class CreateApiKeyRequest(BaseModel):
    user_id: str
    label: str = Field(default="Default key")


class ApiKeyResponse(BaseModel):
    key: str
    user_id: str
    label: str
    tier: str
    is_active: bool


@app.post("/v1/keys", response_model=ApiKeyResponse, status_code=201)
async def create_api_key(body: CreateApiKeyRequest) -> ApiKeyResponse:
    """Issue a new API key for a user (free tier by default)."""
    key = "mcp_" + secrets.token_urlsafe(32)
    record = db.create_api_key(body.user_id, key, body.label)
    return ApiKeyResponse(
        key=record["Key"],
        user_id=record["UserId"],
        label=record["Label"],
        tier=record["Tier"],
        is_active=record["IsActive"],
    )


@app.get("/v1/keys/{user_id}", response_model=list[ApiKeyResponse])
async def list_api_keys(user_id: str) -> list[ApiKeyResponse]:
    records = db.list_api_keys_for_user(user_id)
    return [
        ApiKeyResponse(
            key=r["Key"],
            user_id=r["UserId"],
            label=r["Label"],
            tier=r["Tier"],
            is_active=r["IsActive"],
        )
        for r in records
    ]


# ---------------------------------------------------------------------------
# Billing / Subscription
# ---------------------------------------------------------------------------


class CreateSubscriptionRequest(BaseModel):
    user_id: str
    email: str


class SubscriptionResponse(BaseModel):
    user_id: str
    tier: str
    status: str
    stripe_subscription_id: str | None = None


@app.post(
    "/v1/billing/subscribe/pro", response_model=SubscriptionResponse, status_code=201
)
async def subscribe_pro(body: CreateSubscriptionRequest) -> SubscriptionResponse:
    """Upgrade a user to the Pro (unlimited) tier."""
    existing = db.get_subscription(body.user_id)
    if (
        existing
        and existing.get("Status") == "active"
        and existing.get("Tier") == "pro"
    ):
        raise HTTPException(status_code=409, detail="Already on Pro tier")

    customer_id = billing.create_customer(body.user_id, body.email)
    result = billing.create_pro_subscription(customer_id)
    db.upsert_subscription(
        body.user_id,
        tier="pro",
        stripe_customer_id=customer_id,
        stripe_subscription_id=result["subscription_id"],
        status=result["status"],
    )
    # Update all active keys for this user to pro tier
    for key_record in db.list_api_keys_for_user(body.user_id):
        db.update_api_key_tier(key_record["Key"], "pro", result["subscription_id"])

    return SubscriptionResponse(
        user_id=body.user_id,
        tier="pro",
        status=result["status"],
        stripe_subscription_id=result["subscription_id"],
    )


@app.get("/v1/billing/subscription/{user_id}", response_model=SubscriptionResponse)
async def get_subscription(user_id: str) -> SubscriptionResponse:
    record = db.get_subscription(user_id)
    if not record:
        return SubscriptionResponse(user_id=user_id, tier="free", status="none")
    return SubscriptionResponse(
        user_id=user_id,
        tier=record["Tier"],
        status=record["Status"],
        stripe_subscription_id=record.get("StripeSubscriptionId"),
    )


@app.get("/v1/billing/usage/{user_id}")
async def get_usage(user_id: str) -> dict:
    """Get current month call usage for all keys belonging to a user."""
    year_month = billing.current_year_month()
    keys = db.list_api_keys_for_user(user_id)
    usage = []
    total = 0
    for k in keys:
        count = db.get_call_count(k["Key"], year_month)
        total += count
        usage.append(
            {
                "key": k["Key"][:12] + "...",
                "label": k["Label"],
                "calls": count,
                "tier": k["Tier"],
            }
        )
    return {
        "user_id": user_id,
        "year_month": year_month,
        "total_calls": total,
        "free_tier_limit": settings.free_tier_monthly_calls,
        "keys": usage,
    }


@app.post("/v1/billing/webhook", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(alias="stripe-signature"),
) -> dict:
    import logging

    logger = logging.getLogger(__name__)
    payload = await request.body()
    try:
        event = billing.construct_webhook_event(payload, stripe_signature)
    except Exception as exc:
        logger.warning("Webhook signature failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "customer.subscription.deleted":
        # Downgrade back to free — look up user_id from metadata
        user_id = obj.get("metadata", {}).get("user_id")
        if user_id:
            db.upsert_subscription(
                user_id,
                tier="free",
                stripe_customer_id=obj.get("customer", ""),
                stripe_subscription_id=None,
                status="canceled",
            )
            for key_record in db.list_api_keys_for_user(user_id):
                db.update_api_key_tier(key_record["Key"], "free", None)

    elif event_type == "invoice.payment_failed":
        logger.warning("Payment failed for customer %s", obj.get("customer"))

    return {"received": True}
