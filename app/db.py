"""DynamoDB client and table operations."""

import boto3
from botocore.config import Config
from app.config import settings

_CALL_WINDOW = "month"


def get_table():
    config = Config(region_name=settings.aws_region, retries={"max_attempts": 3, "mode": "adaptive"})
    kwargs: dict = {"config": config}
    if settings.dynamodb_endpoint_url:
        kwargs["endpoint_url"] = settings.dynamodb_endpoint_url
        kwargs["aws_access_key_id"] = "local"
        kwargs["aws_secret_access_key"] = "local"
    return boto3.resource("dynamodb", **kwargs).Table(settings.dynamodb_table_name)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# API key records  PK: APIKEY#<key>  SK: META
# ---------------------------------------------------------------------------

def create_api_key(user_id: str, key: str, label: str) -> dict:
    table = get_table()
    now = _now_iso()
    item = {
        "PK": f"APIKEY#{key}",
        "SK": "META",
        "EntityType": "ApiKey",
        "UserId": user_id,
        "Key": key,
        "Label": label,
        "IsActive": True,
        "Tier": "free",
        "StripeSubscriptionId": None,
        "CreatedAt": now,
        "UpdatedAt": now,
        "GSI1PK": f"USER#{user_id}",
        "GSI1SK": f"APIKEY#{key}",
    }
    table.put_item(Item=item)
    return item


def get_api_key(key: str) -> dict | None:
    table = get_table()
    resp = table.get_item(Key={"PK": f"APIKEY#{key}", "SK": "META"})
    return resp.get("Item")


def update_api_key_tier(key: str, tier: str, stripe_subscription_id: str | None) -> None:
    table = get_table()
    table.update_item(
        Key={"PK": f"APIKEY#{key}", "SK": "META"},
        UpdateExpression="SET Tier = :t, StripeSubscriptionId = :s, UpdatedAt = :u",
        ExpressionAttributeValues={":t": tier, ":s": stripe_subscription_id, ":u": _now_iso()},
    )


def list_api_keys_for_user(user_id: str) -> list[dict]:
    table = get_table()
    resp = table.query(
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :sk)",
        ExpressionAttributeValues={":pk": f"USER#{user_id}", ":sk": "APIKEY#"},
    )
    return resp.get("Items", [])


# ---------------------------------------------------------------------------
# Usage counters  PK: USAGE#<key>#<year-month>  SK: COUNT
# ---------------------------------------------------------------------------

def increment_call_count(key: str, year_month: str) -> int:
    """Atomically increment and return the new call count for key in given month."""
    table = get_table()
    resp = table.update_item(
        Key={"PK": f"USAGE#{key}#{year_month}", "SK": "COUNT"},
        UpdateExpression="ADD CallCount :one SET UpdatedAt = :u",
        ExpressionAttributeValues={":one": 1, ":u": _now_iso()},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["CallCount"])


def get_call_count(key: str, year_month: str) -> int:
    table = get_table()
    resp = table.get_item(Key={"PK": f"USAGE#{key}#{year_month}", "SK": "COUNT"})
    item = resp.get("Item")
    return int(item["CallCount"]) if item else 0


# ---------------------------------------------------------------------------
# Subscription records  PK: USER#<user_id>  SK: SUBSCRIPTION
# ---------------------------------------------------------------------------

def upsert_subscription(user_id: str, tier: str, stripe_customer_id: str, stripe_subscription_id: str | None, status: str) -> dict:
    table = get_table()
    now = _now_iso()
    item = {
        "PK": f"USER#{user_id}",
        "SK": "SUBSCRIPTION",
        "EntityType": "Subscription",
        "UserId": user_id,
        "Tier": tier,
        "StripeCustomerId": stripe_customer_id,
        "StripeSubscriptionId": stripe_subscription_id,
        "Status": status,
        "UpdatedAt": now,
    }
    table.put_item(Item=item)
    return item


def get_subscription(user_id: str) -> dict | None:
    table = get_table()
    resp = table.get_item(Key={"PK": f"USER#{user_id}", "SK": "SUBSCRIPTION"})
    return resp.get("Item")
