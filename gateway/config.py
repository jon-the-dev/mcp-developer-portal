"""Gateway configuration."""

from __future__ import annotations

import json
import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # URL of the billing/portal service (POST /v1/gateway/check)
    billing_service_url: str = "http://localhost:8000"

    # JSON map of server-name -> backend URL, e.g.:
    #   '{"domain-lookup": "http://domain-lookup-mcp:8001"}'
    # Overridden by individual MCP_BACKEND_* env vars at startup.
    backend_servers_json: str = "{}"

    # Timeout (seconds) for upstream MCP requests
    upstream_timeout: float = 30.0

    # Port this gateway listens on
    port: int = 8080

    @field_validator("backend_servers_json")
    @classmethod
    def validate_json(cls, v: str) -> str:
        json.loads(v)  # raises if invalid
        return v

    @property
    def backend_servers(self) -> dict[str, str]:
        """Return merged backend server map.

        Precedence (highest first):
        1. Individual MCP_BACKEND_<NAME>=<url> env vars
        2. BACKEND_SERVERS_JSON env var
        """
        servers: dict[str, str] = json.loads(self.backend_servers_json)
        # Scan for MCP_BACKEND_* overrides
        prefix = "MCP_BACKEND_"
        for key, val in os.environ.items():
            if key.startswith(prefix) and val:
                server_name = key[len(prefix) :].lower().replace("_", "-")
                servers[server_name] = val
        return servers


@lru_cache
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()


gateway_settings = get_gateway_settings()
