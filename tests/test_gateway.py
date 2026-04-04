"""Unit tests for the MCP Gateway.

Mocks out the billing service and backend MCP server so tests run without
external dependencies.
"""

from __future__ import annotations

import pytest
import httpx
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.config import GatewaySettings, get_gateway_settings


# ---------------------------------------------------------------------------
# Test settings
# ---------------------------------------------------------------------------

TEST_SETTINGS = GatewaySettings(
    billing_service_url="http://billing-svc",
    backend_servers_json={test-server: http://mcp-backend},
    upstream_timeout=5.0,
)


@pytest.fixture(autouse=True)
def _override_settings():
    """Inject deterministic test settings via FastAPI dependency override."""
    app.dependency_overrides[get_gateway_settings] = lambda: TEST_SETTINGS
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "mcp-gateway"
    assert "test-server" in data["backends"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_api_key_returns_401(client):
    resp = client.post("/test-server/mcp", json={})
    assert resp.status_code == 401


def test_api_key_via_bearer(client, respx_mock):
    respx_mock.post("http://billing-svc/v1/gateway/check").mock(
        return_value=httpx.Response(
            200, json={"allowed": True, "tier": "free", "calls_this_month": 1, "limit": 1000}
        )
    )
    respx_mock.post("http://mcp-backend/mcp").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    resp = client.post(
        "/test-server/mcp",
        json={"method": "tools/list"},
        headers={"Authorization": "Bearer mcp_test123"},
    )
    assert resp.status_code == 200


def test_api_key_via_x_api_key_header(client, respx_mock):
    respx_mock.post("http://billing-svc/v1/gateway/check").mock(
        return_value=httpx.Response(
            200, json={"allowed": True, "tier": "pro", "calls_this_month": -1, "limit": None}
        )
    )
    respx_mock.post("http://mcp-backend/mcp").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    resp = client.post(
        "/test-server/mcp",
        json={"method": "tools/list"},
        headers={"X-API-Key": "mcp_test456"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limited_returns_429(client, respx_mock):
    respx_mock.post("http://billing-svc/v1/gateway/check").mock(
        return_value=httpx.Response(
            200,
            json={"allowed": False, "tier": "free", "calls_this_month": 1000, "limit": 1000},
        )
    )
    resp = client.post(
        "/test-server/mcp",
        json={},
        headers={"Authorization": "Bearer mcp_overlimit"},
    )
    assert resp.status_code == 429
    assert "Rate limit exceeded" in resp.json()["detail"]


def test_invalid_key_returns_401(client, respx_mock):
    respx_mock.post("http://billing-svc/v1/gateway/check").mock(
        return_value=httpx.Response(401, json={"detail": "invalid"})
    )
    resp = client.post(
        "/test-server/mcp",
        json={},
        headers={"Authorization": "Bearer mcp_bad"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_unknown_server_returns_404(client, respx_mock):
    respx_mock.post("http://billing-svc/v1/gateway/check").mock(
        return_value=httpx.Response(
            200, json={"allowed": True, "tier": "free", "calls_this_month": 0, "limit": 1000}
        )
    )
    resp = client.post(
        "/nonexistent-server/mcp",
        json={},
        headers={"Authorization": "Bearer mcp_valid"},
    )
    assert resp.status_code == 404
    assert "nonexistent-server" in resp.json()["detail"]


def test_proxy_passes_body_and_returns_upstream_status(client, respx_mock):
    respx_mock.post("http://billing-svc/v1/gateway/check").mock(
        return_value=httpx.Response(
            200, json={"allowed": True, "tier": "free", "calls_this_month": 5, "limit": 1000}
        )
    )
    respx_mock.post("http://mcp-backend/mcp/tools/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}]})
    )
    resp = client.post(
        "/test-server/mcp/tools/call",
        json={"name": "ping", "arguments": {}},
        headers={"Authorization": "Bearer mcp_valid"},
    )
    assert resp.status_code == 200


def test_billing_service_down_returns_503(client, respx_mock):
    respx_mock.post("http://billing-svc/v1/gateway/check").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    resp = client.post(
        "/test-server/mcp",
        json={},
        headers={"Authorization": "Bearer mcp_valid"},
    )
    assert resp.status_code == 503


def test_gateway_tier_header_in_response(client, respx_mock):
    respx_mock.post("http://billing-svc/v1/gateway/check").mock(
        return_value=httpx.Response(
            200, json={"allowed": True, "tier": "pro", "calls_this_month": -1, "limit": None}
        )
    )
    respx_mock.post("http://mcp-backend/mcp").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    resp = client.post(
        "/test-server/mcp",
        json={},
        headers={"Authorization": "Bearer mcp_pro"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-gateway-tier") == "pro"
