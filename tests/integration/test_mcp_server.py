"""Integration tests for the MCP server (Streamable HTTP transport)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch

from querygate.api.app import create_app
from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.execution import concurrency as cc
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy

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
    "search_catalog",
    "run_structured_queries",
    "run_structured_writes",
    "search_querygate_guide",
    "get_querygate_guide_topic",
    "get_querygate_setup_checklist",
    "explain_querygate_config_field",
    "explain_querygate_error",
    "describe_my_querygate_access",
    "inspect_querygate_configuration",
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
    # TODO.md item 63: the dev-bypass anonymous principal has no scopes, so
    # the admin-only inspect_querygate_configuration tool is filtered out of
    # tools/list — it would only ever reject that principal at call time
    # anyway (see test_mcp_tools_list_omits_scope_gated_tool_without_scope).
    resp = await mcp_dev_client.post("/mcp/", json=_TOOLS_LIST, headers=_HEADERS_JSON)
    assert resp.status_code == 200
    payload = _parse_mcp_response(resp)
    tool_names = {tool["name"] for tool in payload["result"]["tools"]}
    assert (_EXPECTED_TOOLS - {"inspect_querygate_configuration"}).issubset(tool_names)
    assert "inspect_querygate_configuration" not in tool_names


@pytest.mark.asyncio
async def test_mcp_product_guide_search_returns_packaged_versioned_citations(mcp_dev_client):
    resp = await mcp_dev_client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "search_querygate_guide",
                "arguments": {"query": "configure policy limits", "limit": 3},
            },
        },
        headers=_HEADERS_JSON,
    )

    assert resp.status_code == 200
    result = _parse_mcp_response(resp)["result"]["structuredContent"]["result"]
    assert result["querygate_version"] == "0.1.0"
    assert result["results"][0]["topic_id"] == "configuration.policy"
    assert result["results"][0]["citation"]["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_mcp_configuration_inspection_requires_config_read_scope():
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_key_scopes=[])
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
                "id": 21,
                "method": "tools/call",
                "params": {"name": "inspect_querygate_configuration", "arguments": {}},
            },
            headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
        )

    result = _parse_mcp_response(resp)["result"]["structuredContent"]["result"]
    assert result["success"] is False
    assert result["error_code"] == "FORBIDDEN"
    assert result["error_message"] == "Missing required scope: 'admin:config:read'"


@pytest.mark.asyncio
async def test_mcp_tools_list_omits_scope_gated_tool_without_scope():
    """TODO.md item 63: inspect_querygate_configuration's ~6KB schema is not
    sent to a session that can never call it — visibility only, the real
    boundary stays the call-time scope check proven by the test above. A
    non-admin-scoped session still sees every other tool.
    """
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_key_scopes=[])
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        resp = await client.post(
            "/mcp/",
            json=_TOOLS_LIST,
            headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
        )

    tool_names = {tool["name"] for tool in _parse_mcp_response(resp)["result"]["tools"]}
    assert "inspect_querygate_configuration" not in tool_names
    assert (_EXPECTED_TOOLS - {"inspect_querygate_configuration"}).issubset(tool_names)


@pytest.mark.asyncio
async def test_mcp_tools_list_includes_scope_gated_tool_with_scope():
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_key_scopes=["admin:config:read"])
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        resp = await client.post(
            "/mcp/",
            json=_TOOLS_LIST,
            headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
        )

    tool_names = {tool["name"] for tool in _parse_mcp_response(resp)["result"]["tools"]}
    assert "inspect_querygate_configuration" in tool_names
    assert _EXPECTED_TOOLS.issubset(tool_names)


@pytest.mark.asyncio
async def test_mcp_configuration_inspection_uses_request_application_config(tmp_path):
    connections_file = tmp_path / "guide-connections.yaml"
    connections_file.write_text("""
connections:
  - id: request-bound-config
    dialect: postgresql
    connection_string: ${REQUEST_BOUND_DATABASE_URL}
""")
    policy_file = tmp_path / "guide-policy.yaml"
    policy_file.write_text("default:\n  enabled: true\n")
    _reset_mcp_session_manager()
    settings = _mcp_settings(
        connections_file=str(connections_file),
        policy_file=str(policy_file),
        mcp_api_key_scopes=["admin:config:read"],
    )
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
                "id": 22,
                "method": "tools/call",
                "params": {"name": "inspect_querygate_configuration", "arguments": {}},
            },
            headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
        )

    result = _parse_mcp_response(resp)["result"]["structuredContent"]["result"]
    assert [connection["id"] for connection in result["connections"]] == ["request-bound-config"]
    assert "REQUEST_BOUND_DATABASE_URL" not in json.dumps(result)


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
        access_response = await client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "describe_my_querygate_access", "arguments": {}},
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

    access = _parse_mcp_response(access_response)["result"]["structuredContent"]["result"]
    assert [connection["id"] for connection in access["visible_connections"]] == ["demo"]
    assert "internal_finance" not in json.dumps(access)


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
async def test_mcp_describe_table_includes_catalog_metadata_when_configured():
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {
                        "tables": {
                            "customers": {
                                "description": "One row per customer.",
                                "sensitivity": "internal",
                                "columns": {
                                    "email": {"description": "Email address", "sensitivity": "pii"}
                                },
                            }
                        }
                    }
                }
            }
        )
    )
    customers = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(100)),
    )
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[_TEST_API_KEY])
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
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "describe_table",
                        "arguments": {"connection": "demo", "table_name": "customers"},
                    },
                },
                headers={**_HEADERS_JSON, "Authorization": f"Bearer {_TEST_API_KEY}"},
            )
    assert resp.status_code == 200
    result = _parse_mcp_response(resp)["result"]["structuredContent"]["result"]
    assert result["catalog"]["description"] == "One row per customer."
    email_column = next(c for c in result["columns"] if c["name"] == "email")
    assert email_column["catalog"]["sensitivity"] == "pii"


@pytest.mark.asyncio
async def test_mcp_catalog_search_is_compact_policy_filtered_and_cited():
    set_catalog_store(
        CatalogStore.from_dict(
            {
                "version": 2,
                "connections": {
                    "demo": {
                        "tables": {
                            "orders": {
                                "description": "Purchases and recognized revenue.",
                                "aliases": ["sales"],
                            }
                        }
                    }
                },
            }
        )
    )
    _reset_mcp_session_manager()
    app = create_app(_mcp_settings(mcp_api_keys=[]))
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
                "id": 23,
                "method": "tools/call",
                "params": {
                    "name": "search_catalog",
                    "arguments": {
                        "connection": "demo",
                        "query": "sales revenue",
                        "limit": 2,
                        "verbose_provenance": True,
                    },
                },
            },
            headers=_HEADERS_JSON,
        )

    assert resp.status_code == 200
    result = _parse_mcp_response(resp)["result"]["structuredContent"]["result"]
    assert result["result_count"] == 1
    assert result["results"][0]["table"] == "orders"
    assert result["results"][0]["citation"]["source_class"] == "verified"
    assert result["results"][0]["citation"]["schema_fingerprint"] == "untracked"


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
                    "name": "run_structured_queries",
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


@pytest.mark.asyncio
async def test_mcp_run_structured_queries_explain_mode_never_executes():
    """TODO.md item 61: run_structured_queries(mode="explain") replaces the
    old explain_structured_query tool — must still never open a DB session
    or touch the concurrency limiter, only compile.
    """
    customers = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100)),
    )
    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[])
    app = create_app(settings)
    with (
        patch("querygate.execution.service.get_engine", return_value=MagicMock()),
        patch(
            "querygate.validation.schema_validation.get_table_schema",
            AsyncMock(return_value=customers),
        ),
        patch("querygate.execution.service.session_scope") as mock_scope,
    ):
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
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "run_structured_queries",
                        "arguments": {
                            "connection": "demo",
                            "queries": [
                                {"from": "customers", "select": ["customers.id"], "limit": 5}
                            ],
                            "mode": "explain",
                        },
                    },
                },
                headers=_HEADERS_JSON,
            )
    mock_scope.assert_not_called()
    assert resp.status_code == 200
    result = _parse_mcp_response(resp)["result"]["structuredContent"]["result"]
    assert "customers" in result["results"][0]["sql"]
    assert result["results"][0]["error"] is None


@pytest.mark.asyncio
async def test_mcp_run_structured_queries_isolates_per_item_failure():
    """One invalid query in the list must not fail the others (default
    mode="execute") — same batch semantics the old execute_structured_queries
    tool had, now the only way to call this tool.
    """
    customers = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
    )
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [{"id": 1}]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[])
    app = create_app(settings)
    with (
        patch("querygate.execution.service.get_engine", return_value=MagicMock()),
        patch(
            "querygate.validation.schema_validation.get_table_schema",
            AsyncMock(return_value=customers),
        ),
        patch("querygate.execution.service.session_scope", _scope),
    ):
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
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "run_structured_queries",
                        "arguments": {
                            "connection": "demo",
                            "queries": [
                                {"from": "customers", "select": ["customers.id"], "limit": 5},
                                {"from": "not_a_real_table", "select": ["x.id"], "limit": 5},
                            ],
                        },
                    },
                },
                headers=_HEADERS_JSON,
            )
    assert resp.status_code == 200
    result = _parse_mcp_response(resp)["result"]["structuredContent"]["result"]
    assert len(result["results"]) == 2
    assert result["results"][0]["error"] is None
    assert result["results"][0]["row_count"] == 1
    assert result["results"][1]["error"] is not None


@pytest.mark.asyncio
async def test_mcp_execute_fail_fast_reports_capacity_timeout_with_admission_fields():
    """TODO.md item 35 phase 1: queue_mode="fail_fast" must reject immediately
    (never waiting for a slot) and surface a stable admission_id plus a
    machine-readable capacity_timeout state — not just a generic VALIDATION
    error indistinguishable from a bad query.
    """
    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=1, concurrency_wait_seconds=5), overrides={})
    )
    await cc.in_process_limiter().semaphore("demo", 1).acquire()  # occupy the only slot

    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[], concurrency_backend="in_process")
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
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "run_structured_queries",
                    "arguments": {
                        "connection": "demo",
                        "queries": [{"from": "customers", "select": ["customers.id"], "limit": 5}],
                        "queue_mode": "fail_fast",
                    },
                },
            },
            headers=_HEADERS_JSON,
        )
    assert resp.status_code == 200
    payload = _parse_mcp_response(resp)
    result = payload["result"]["structuredContent"]["result"]
    # A single query is a batch of one — a capacity timeout on one query in
    # the batch is a per-item error, not a top-level tool failure (the tool
    # call itself succeeded; see TODO.md item 61's merge).
    assert result["results"][0]["error"] is not None
    assert "too many concurrent" in result["results"][0]["error"]
    assert result["results"][0]["admission_state"] == "capacity_timeout"
    assert result["results"][0]["admission_id"]
    assert result["results"][0]["queue_wait_ms"] is not None


@pytest.mark.asyncio
async def test_mcp_execute_reports_queue_full_when_max_queue_depth_is_met():
    """TODO.md item 35 phase 2: Policy.max_queue_depth caps how many callers
    may be *waiting* for a slot at once, distinct from max_concurrency
    (which caps how many may *run*). A caller rejected by this cap gets a
    distinct queue_full admission_state, not a generic VALIDATION error
    indistinguishable from a genuine wait-timeout.
    """
    set_policy_store(
        PolicyStore(
            default=Policy(max_concurrency=1, concurrency_wait_seconds=5, max_queue_depth=1),
            overrides={},
        )
    )
    await cc.in_process_limiter().semaphore("demo", 1).acquire()  # occupy the only slot

    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[], concurrency_backend="in_process")
    app = create_app(settings)

    def _wait_call(call_id: int) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {
                "name": "run_structured_queries",
                "arguments": {
                    "connection": "demo",
                    "queries": [{"from": "customers", "select": ["customers.id"], "limit": 5}],
                    "queue_mode": "wait",
                    "wait_timeout_seconds": 5,
                },
            },
        }

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE_URL, follow_redirects=True
        ) as client,
    ):
        first_task = asyncio.create_task(
            client.post("/mcp/", json=_wait_call(7), headers=_HEADERS_JSON)
        )
        await asyncio.sleep(0.05)  # let it actually start waiting (queue depth == 1)

        resp = await client.post("/mcp/", json=_wait_call(8), headers=_HEADERS_JSON)
        assert resp.status_code == 200
        payload = _parse_mcp_response(resp)
        result = payload["result"]["structuredContent"]["result"]
        # Single query = batch of one; a queue-full rejection is a per-item
        # error, not a top-level tool failure (see TODO.md item 61's merge).
        assert result["results"][0]["admission_state"] == "queue_full"
        assert result["results"][0]["admission_id"]
        assert result["results"][0]["queue_wait_ms"] == 0

        cc.in_process_limiter().semaphore("demo", 1).release()
        await first_task


@pytest.mark.asyncio
async def test_mcp_run_structured_queries_accepts_a_range_join_and_gates_cross():
    """Item 103's join forms over the MCP transport, not just REST.

    The AST shape is reachable through `run_structured_queries` as well as the
    REST route, and the two are thin wrappers over one service — but "the schema
    accepts it and the pipeline compiles it" is a transport-level claim that only
    a transport-level test settles. Both halves are asserted in one round trip:
    a `condition` join compiles to real SQL, and a `cross` join is refused by the
    default policy rather than silently executed.
    """
    metadata = sa.MetaData()
    products = sa.Table(
        "products",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("price", sa.Numeric(10, 2)),
    )

    _reset_mcp_session_manager()
    settings = _mcp_settings(mcp_api_keys=[])
    app = create_app(settings)
    with (
        patch("querygate.execution.service.get_engine", return_value=MagicMock()),
        patch(
            "querygate.validation.schema_validation.get_table_schema",
            AsyncMock(return_value=products),
        ),
    ):
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
                    "id": 21,
                    "method": "tools/call",
                    "params": {
                        "name": "run_structured_queries",
                        "arguments": {
                            "connection": "demo",
                            "mode": "explain",
                            "queries": [
                                {
                                    "from": "products",
                                    "from_alias": "p",
                                    "select": ["p.id"],
                                    "joins": [
                                        {
                                            "table": "products",
                                            "alias": "band",
                                            "condition": {
                                                "col": "p.price",
                                                "op": "gte",
                                                "value_col": "band.price",
                                            },
                                        }
                                    ],
                                    "limit": 5,
                                },
                                {
                                    "from": "products",
                                    "from_alias": "a",
                                    "select": ["a.id"],
                                    "joins": [{"table": "products", "alias": "b", "type": "cross"}],
                                    "limit": 5,
                                },
                            ],
                        },
                    },
                },
                headers=_HEADERS_JSON,
            )

    assert resp.status_code == 200
    results = _parse_mcp_response(resp)["result"]["structuredContent"]["result"]["results"]

    # The range join reached the compiler and rendered its inequality.
    assert results[0]["error"] is None, results[0]
    assert "p.price >= band.price" in results[0]["sql"]

    # The cross join was refused by policy, over the same transport, in the same batch.
    assert results[1]["error"] is not None
    assert "allow_cross_join" in results[1]["error"]
