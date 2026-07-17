"""Integration tests for the MCP server (Streamable HTTP transport)."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_TEST_API_KEY = "integration-test-mcp-key"
_BASE_URL = "http://localhost"
_HEADERS_JSON = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
_EXPECTED_TOOLS = {
    "list_connections",
    "list_tables",
    "describe_table",
    "explain_structured_query",
    "execute_structured_query",
    "execute_structured_queries",
}


def _parse_mcp_response(resp) -> dict:
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise ValueError(f"No data line found in SSE response: {resp.text!r}")


def _reset_mcp_session_manager() -> None:
    from querygate.mcp.server import mcp_server

    mcp_server._session_manager = None  # type: ignore[assignment]


def _mcp_settings(**overrides) -> AppConfig:
    base = dict(
        environment="localhost",
        mcp_enabled=True,
        mcp_api_keys=[_TEST_API_KEY],
        mcp_mount_path="/mcp",
    )
    base.update(overrides)
    return AppConfig(**base)


@pytest_asyncio.fixture
async def mcp_dev_client():
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[])
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        yield client


@pytest.mark.asyncio
async def test_mcp_disabled_returns_404():
    settings = _mcp_settings(mcp_enabled=False)
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/mcp/", json=_TOOLS_LIST, headers=_HEADERS_JSON)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mcp_dev_bypass_lists_tools(mcp_dev_client):
    resp = await mcp_dev_client.post("/mcp/", json=_TOOLS_LIST, headers=_HEADERS_JSON)
    assert resp.status_code == 200
    payload = _parse_mcp_response(resp)
    tool_names = {tool["name"] for tool in payload["result"]["tools"]}
    assert _EXPECTED_TOOLS.issubset(tool_names)


@pytest.mark.asyncio
async def test_mcp_api_key_required_when_configured():
    _reset_mcp_session_manager()
    settings = _mcp_settings()
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        unauthorized = await client.post("/mcp/", json=_TOOLS_LIST, headers=_HEADERS_JSON)
        assert unauthorized.status_code == 401

        authorized = await client.post(
            "/mcp/",
            json=_TOOLS_LIST,
            headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
        )
        assert authorized.status_code == 200


@pytest.mark.asyncio
async def test_list_connections_tool_never_leaks_credentials(mcp_dev_client):
    resp = await mcp_dev_client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_connections", "arguments": {}},
        },
        headers=_HEADERS_JSON,
    )
    assert resp.status_code == 200
    payload = _parse_mcp_response(resp)
    connections = payload["result"]["structuredContent"]["result"]["connections"]
    assert connections[0]["id"] == "demo"
    assert "connection_string" not in connections[0]
