"""Application configuration."""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    app_name: str = "MCP Developer Portal"
    environment: str = "development"

    # DynamoDB
    dynamodb_table_name: str = "mcp-portal-dev"
    dynamodb_endpoint_url: str | None = None
    aws_region: str = "us-east-1"

    # JWT
    jwt_secret_key: str = "CHANGE-ME"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # Stripe
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_free_price_id: str | None = None  # $0/month
    stripe_pro_price_id: str | None = None  # unlimited calls flat rate

    # Tier limits
    free_tier_monthly_calls: int = 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
