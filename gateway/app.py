"""MCP Gateway — FastAPI reverse proxy with API key auth and call-based rate limiting.

Architecture:
  Client  ->  Gateway (this service)  ->  Billing service (/v1/gateway/check)
                                      ->  Backend MCP server (proxied)

Auth: API key passed as ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``
Rate limiting: delegated to billing service (free tier: 1 000 calls/month)
Routing: ``/{server_name}/{path:path}`` -> backend URL from config
Streaming: SSE responses are streamed transparently (MCP streamable-HTTP transport)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, AsyncGenerator

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from gateway.config import GatewaySettings, get_gateway_settings

logger = logging.getLogger(__name__)

app = FastAPI(
    title="MCP Gateway",
    version="0.1.0",
    description="Authenticated reverse proxy for hosted MCP servers",
)

Settings = Annotated[GatewaySettings, Depends(get_gateway_settings)]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health(settings: Settings) -> dict[str, Any]:
    return {
        "status": "healthy",
        "service": "mcp-gateway",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backends": list(settings.backend_servers.keys()),
    }


# ---------------------------------------------------------------------------
# Proxy route
# ---------------------------------------------------------------------------


@app.api_route(
    "/{server_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_to_mcp_server(
    server_name: str,
    path: str,
    request: Request,
    settings: Settings,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> Response:
    """Authenticate, rate-limit, then proxy the request to the named MCP backend."""

    # 1. Extract API key (prefer explicit X-API-Key, fall back to Bearer token)
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization.split(" ", 1)[1]
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="API key required — pass 'Authorization: Bearer <key>' or 'X-API-Key: <key>'",
        )

    # 2. Validate key and check rate limit via billing service
    check_data = await _billing_check(api_key, settings.billing_service_url)
    if not check_data.get("allowed"):
        used = check_data.get("calls_this_month", 0)
        limit = check_data.get("limit") or "unlimited"
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {used}/{limit} calls used this month. Upgrade to Pro for unlimited calls.",
        )

    tier = check_data.get("tier", "free")

    # 3. Resolve backend
    backend_url = settings.backend_servers.get(server_name)
    if not backend_url:
        available = sorted(settings.backend_servers.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown MCP server '{server_name}'. Available: {available}",
        )

    # 4. Build upstream URL
    upstream_path = path.lstrip("/")
    upstream_url = f"{backend_url.rstrip('/')}/{upstream_path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    # 5. Forward headers — strip auth/host, add gateway metadata
    forward_headers: dict[str, str] = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("authorization", "x-api-key", "host", "content-length")
    }
    forward_headers["x-gateway-tier"] = tier
    forward_headers["x-forwarded-host"] = request.headers.get("host", "")
    if request.client:
        forward_headers["x-forwarded-for"] = request.client.host

    body = await request.body()
    accept = request.headers.get("accept", "")

    # 6. Proxy — stream SSE, otherwise buffer
    if "text/event-stream" in accept:
        return StreamingResponse(
            _stream_upstream(request.method, upstream_url, forward_headers, body, settings.upstream_timeout),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Gateway-Tier": tier,
            },
        )

    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=upstream_url,
                headers=forward_headers,
                content=body,
            )
        except httpx.RequestError as exc:
            logger.error("Upstream %s error: %s", upstream_url, exc)
            raise HTTPException(status_code=502, detail=f"Backend '{server_name}' unreachable")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
        headers={"X-Gateway-Tier": tier},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _billing_check(api_key: str, billing_url: str) -> dict[str, Any]:
    """Call the billing service to validate the key and check the rate limit."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.post(
                f"{billing_url}/v1/gateway/check",
                json={"api_key": api_key},
            )
        except httpx.RequestError as exc:
            logger.error("Billing service unreachable: %s", exc)
            raise HTTPException(
                status_code=503, detail="Gateway billing service temporarily unavailable"
            )

    if resp.status_code == 200:
        return resp.json()
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    logger.error("Billing service returned %s: %s", resp.status_code, resp.text)
    raise HTTPException(status_code=502, detail="Billing service error")


async def _stream_upstream(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> AsyncGenerator[bytes, None]:
    """Yield chunks from an SSE upstream response."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream(method, url, headers=headers, content=body) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=256):
                    yield chunk
        except httpx.RequestError as exc:
            logger.error("SSE upstream error: %s", exc)
            yield b"event: error\ndata: upstream connection failed\n\n"
