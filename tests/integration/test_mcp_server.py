"""Integration tests for the MCP server (Streamable HTTP transport)."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch

from querygate.api.app import create_app
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.policy.loader import PolicyStore, set_policy_store

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
        audit_sink_backend="none",
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


@pytest.mark.asyncio
async def test_mcp_connection_listing_and_direct_access_are_principal_scoped():
    set_registry(
        ConnectionRegistry(
            {
                connection_id: ConnectionProfile(
                    id=connection_id,
                    dialect="postgresql",
                    connection_string=f"postgresql+asyncpg://user:pass@localhost/{connection_id}",
                    known_tables=["customers"],
                )
                for connection_id in ("demo", "internal_finance")
            }
        )
    )
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"enabled": False},
                "principals": {"agent-a": {"demo": {"enabled": True}}},
            }
        )
    )
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[_TEST_API_KEY], mcp_api_key_subject="agent-a")
    app = create_app(settings)
    auth_headers = {**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        listed_response = await client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "list_connections", "arguments": {}},
            },
            headers=auth_headers,
        )
        hidden_response = await client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "list_tables",
                    "arguments": {"connection": "internal_finance"},
                },
            },
            headers=auth_headers,
        )

    listed = _parse_mcp_response(listed_response)
    connections = listed["result"]["structuredContent"]["result"]["connections"]
    assert [connection["id"] for connection in connections] == ["demo"]

    hidden = _parse_mcp_response(hidden_response)
    error = hidden["result"]["structuredContent"]["result"]
    assert error["success"] is False
    assert error["error_code"] == "NOT_FOUND"
    assert error["error_message"] == "Unknown connection: 'internal_finance'"


@pytest.mark.asyncio
async def test_mcp_list_tables_uses_per_principal_policy():
    """Regression: MCP schema tools used to instantiate
    StructuredQueryService without the authenticated MCP caller, exposing
    connection-level schema rather than the caller-filtered view.
    """
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {},
                "principals": {"agent-a": {"demo": {"denied_tables": ["order_items"]}}},
            }
        )
    )
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[_TEST_API_KEY], mcp_api_key_subject="agent-a")
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        resp = await client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_tables", "arguments": {"connection": "demo"}},
            },
            headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
        )
    assert resp.status_code == 200
    payload = _parse_mcp_response(resp)
    tables = payload["result"]["structuredContent"]["result"]["tables"]
    assert "customers" in tables
    assert "order_items" not in tables


@pytest.mark.asyncio
async def test_mcp_describe_table_uses_per_principal_column_policy():
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {},
                "principals": {"agent-a": {"demo": {"denied_columns": {"customers": ["email"]}}}},
            }
        )
    )
    customers = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(100)),
        sa.Column("name", sa.String(100)),
    )
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[_TEST_API_KEY], mcp_api_key_subject="agent-a")
    app = create_app(settings)
    with (
        patch("querygate.execution.service.get_engine", return_value=MagicMock()),
        patch(
            "querygate.execution.service.get_table_schema",
            AsyncMock(return_value=customers),
        ),
    ):
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url=_BASE_URL,
                follow_redirects=True,
            ) as client,
        ):
            resp = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "describe_table",
                        "arguments": {"connection": "demo", "table_name": "customers"},
                    },
                },
                headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
            )
    assert resp.status_code == 200
    payload = _parse_mcp_response(resp)
    columns = payload["result"]["structuredContent"]["result"]["columns"]
    assert {column["name"] for column in columns} == {"id", "name"}


@pytest.mark.asyncio
async def test_mcp_batch_uses_per_principal_max_batch_size():
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"max_batch_size": 10},
                "principals": {"agent-a": {"demo": {"max_batch_size": 1}}},
            }
        )
    )
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[_TEST_API_KEY], mcp_api_key_subject="agent-a")
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        resp = await client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "execute_structured_queries",
                    "arguments": {
                        "connection": "demo",
                        "queries": [
                            {
                                "from": "customers",
                                "select": ["customers.id"],
                                "limit": 5,
                            },
                            {"from": "orders", "select": ["orders.id"], "limit": 5},
                        ],
                    },
                },
            },
            headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
        )
    assert resp.status_code == 200
    payload = _parse_mcp_response(resp)
    result = payload["result"]["structuredContent"]["result"]
    assert result["success"] is False
    assert result["error_code"] == "VALIDATION"
    assert "max of 1" in result["error_message"]
