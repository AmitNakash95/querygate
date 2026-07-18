"""Integration tests for the REST API surface."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.core.exceptions import QueryValidationError
from querygate.execution.service import (
    BatchQueryItemResult,
    ColumnInfo,
    StructuredQueryResult,
    TableDescription,
)
from querygate.policy.loader import PolicyStore, set_policy_store

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_SERVICE = "querygate.api.routes.StructuredQueryService"


def _settings(**overrides) -> AppConfig:
    base = dict(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
    )
    base.update(overrides)
    return AppConfig(**base)


@pytest.fixture
def app():
    return create_app(_settings())


@pytest.mark.asyncio
async def test_list_connections(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/connections")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["id"] == "demo"
    assert "connection_string" not in body[0]


@pytest.mark.asyncio
async def test_list_connections_and_direct_access_are_principal_scoped(app):
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
    # Local development resolves to subject "anonymous-dev". A disabled
    # default plus explicit grants is the strict deny-by-default setup.
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"enabled": False},
                "principals": {"anonymous-dev": {"demo": {"enabled": True}}},
            }
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        listed = await client.get("/api/v1/connections")
        hidden_schema = await client.get("/api/v1/internal_finance/tables")

    assert [connection["id"] for connection in listed.json()] == ["demo"]
    assert hidden_schema.status_code == 404
    assert hidden_schema.json()["detail"] == "Unknown connection: 'internal_finance'"


@pytest.mark.asyncio
async def test_query_success(app):
    mock_result = StructuredQueryResult(
        rows=[{"id": 1, "name": "Ada"}], row_count=1, truncated=False, limit=50, offset=0
    )
    with patch(f"{_SERVICE}.execute", new_callable=AsyncMock, return_value=mock_result):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query",
                json={
                    "from": "customers",
                    "select": ["customers.id", "customers.name"],
                    "limit": 50,
                },
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == 1
    assert body["rows"][0]["name"] == "Ada"


@pytest.mark.asyncio
async def test_query_unknown_connection_is_404(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/nonexistent/query", json={"from": "customers", "select": ["customers.id"]}
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_query_validation_error_is_422(app):
    with patch(
        f"{_SERVICE}.execute",
        new_callable=AsyncMock,
        side_effect=QueryValidationError("Column 'x' not found"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query", json={"from": "customers", "select": ["customers.x"]}
            )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_no_raw_sql_field_accepted(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/query", json={"sql": "SELECT * FROM customers"})
    # StructuredQuery has no `sql` field and forbids unknown fields — this
    # never reaches the database, it fails FastAPI's own body validation.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_tables(app):
    with patch(
        f"{_SERVICE}.list_tables", new_callable=AsyncMock, return_value=["customers", "orders"]
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.get("/api/v1/demo/tables")
    assert resp.status_code == 200
    assert resp.json()["tables"] == ["customers", "orders"]


@pytest.mark.asyncio
async def test_describe_table(app):
    desc = TableDescription(
        name="customers", columns=[ColumnInfo(name="id", type="INTEGER", nullable=False)]
    )
    with patch(f"{_SERVICE}.describe_table", new_callable=AsyncMock, return_value=desc):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.get("/api/v1/demo/tables/customers")
    assert resp.status_code == 200
    assert resp.json()["name"] == "customers"


@pytest.mark.asyncio
async def test_batch_query(app):
    results = [
        BatchQueryItemResult(rows=[{"id": 1}], row_count=1, truncated=False, limit=5, offset=0)
    ]
    with patch(f"{_SERVICE}.execute_many", new_callable=AsyncMock, return_value=results):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/batch",
                json={"queries": [{"from": "customers", "select": ["customers.id"], "limit": 5}]},
            )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["row_count"] == 1


@pytest.mark.asyncio
async def test_batch_query_uses_per_principal_max_batch_size(app):
    """Regression: batch-size validation used to read only the connection
    policy, so a tighter principal override could be bypassed before
    execute_many() ran.
    """
    set_policy_store(
        PolicyStore.from_dict(
            {
                "default": {"max_batch_size": 10},
                "principals": {"anonymous-dev": {"demo": {"max_batch_size": 1}}},
            }
        )
    )
    with patch(f"{_SERVICE}.execute_many", new_callable=AsyncMock) as execute_many:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/demo/query/batch",
                json={
                    "queries": [
                        {"from": "customers", "select": ["customers.id"], "limit": 5},
                        {"from": "orders", "select": ["orders.id"], "limit": 5},
                    ]
                },
            )
    assert resp.status_code == 422
    assert "max of 1" in resp.json()["detail"]
    execute_many.assert_not_called()


@pytest.mark.asyncio
async def test_auth_required_outside_local_dev():
    settings = _settings(environment="staging", api_keys=["secret-key"])
    staged_app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=staged_app), base_url=_BASE_URL) as client:
        unauthorized = await client.get("/api/v1/connections")
        assert unauthorized.status_code == 401

        authorized = await client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer secret-key"}
        )
        assert authorized.status_code == 200


@pytest.mark.asyncio
async def test_jwt_bearer_token_accepted_alongside_api_key():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    settings = _settings(
        environment="staging",
        api_keys=["secret-key"],
        jwt_enabled=True,
        jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
        jwt_issuer="https://idp.example.com/",
        jwt_audience="querygate",
    )
    jwt_app = create_app(settings)

    token = jwt.encode(
        {"sub": "human-user", "iss": "https://idp.example.com/", "aud": "querygate"},
        private_key,
        algorithm="RS256",
    )

    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(transport=ASGITransport(app=jwt_app), base_url=_BASE_URL) as client:
            jwt_resp = await client.get(
                "/api/v1/connections", headers={"Authorization": f"Bearer {token}"}
            )
            api_key_resp = await client.get(
                "/api/v1/connections", headers={"Authorization": "Bearer secret-key"}
            )
            rejected_resp = await client.get(
                "/api/v1/connections", headers={"Authorization": "Bearer garbage"}
            )
    assert jwt_resp.status_code == 200
    assert api_key_resp.status_code == 200
    assert rejected_resp.status_code == 401


@pytest.mark.asyncio
async def test_jwt_enabled_in_local_dev_still_rejects_invalid_tokens():
    """Regression test: enabling JWT auth in a local/dev environment (the
    default) with no api_keys configured must not silently fall back to the
    anonymous-dev bypass for an invalid/missing token — that would make
    configuring JWT auth in dev a no-op. Caught by live testing during
    development; see core/auth.py's AnonymousAuthenticator docstring.
    """
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    settings = _settings(  # environment defaults to "localhost" (is_local)
        jwt_enabled=True,
        jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
        jwt_issuer="https://idp.example.com/",
        jwt_audience="querygate",
    )
    jwt_app = create_app(settings)

    token_a = jwt.encode(
        {"sub": "agent-a", "iss": "https://idp.example.com/", "aud": "querygate"},
        private_key,
        algorithm="RS256",
    )

    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        lambda self, tok: type("K", (), {"key": public_key})(),
    ):
        async with AsyncClient(transport=ASGITransport(app=jwt_app), base_url=_BASE_URL) as client:
            no_token = await client.get("/api/v1/connections")
            garbage = await client.get(
                "/api/v1/connections", headers={"Authorization": "Bearer garbage"}
            )
            valid = await client.get(
                "/api/v1/connections", headers={"Authorization": f"Bearer {token_a}"}
            )
    assert no_token.status_code == 401
    assert garbage.status_code == 401
    assert valid.status_code == 200


@pytest.mark.asyncio
async def test_metrics_endpoint(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "querygate_queries_total" in resp.text
    assert "querygate_concurrency_in_use" in resp.text


@pytest.mark.asyncio
async def test_health_endpoint(app):
    # /health reads app.state.health_monitor, populated by the app's
    # lifespan — ASGITransport doesn't trigger startup/shutdown on its own
    # (see test_mcp_server.py for the same pattern). health.ping is patched
    # so lifespan startup doesn't attempt a real network connection.
    with patch("querygate.health._ping", new_callable=AsyncMock):
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client,
        ):
            resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "querygate"
    assert body["status"] == "ok"
    assert body["connections"] == {"healthy": 1, "unhealthy": 0, "unknown": 0}
    assert "demo" not in resp.text


@pytest.mark.asyncio
async def test_health_endpoint_reports_degraded_when_connection_unreachable(app):
    with patch(
        "querygate.health._ping", new_callable=AsyncMock, side_effect=ConnectionError("refused")
    ):
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client,
        ):
            resp = await client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["connections"] == {"healthy": 0, "unhealthy": 1, "unknown": 0}
    assert "demo" not in resp.text
    assert "refused" not in resp.text


def _write_reload_config_files(tmp_path):
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text(
        """
connections:
  - id: reload-demo
    dialect: postgresql
    connection_string: ${TEST_RELOAD_DB_URL}
    known_tables: [foo]
"""
    )
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        """
default:
  enabled: true
"""
    )
    return str(connections_file), str(policy_file)


@pytest.mark.asyncio
async def test_reload_config_forbidden_without_scope(tmp_path):
    connections_file, policy_file = _write_reload_config_files(tmp_path)
    settings = _settings(
        api_keys=["secret-key"], connections_file=connections_file, policy_file=policy_file
    )
    reload_app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=reload_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/reload-config", headers={"Authorization": "Bearer secret-key"}
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reload_config_succeeds_with_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_RELOAD_DB_URL", "postgresql+asyncpg://user:pass@localhost/demo")
    connections_file, policy_file = _write_reload_config_files(tmp_path)
    settings = _settings(
        api_keys=["secret-key"],
        api_key_scopes=["admin:reload-config"],
        connections_file=connections_file,
        policy_file=policy_file,
    )
    reload_app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=reload_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/reload-config", headers={"Authorization": "Bearer secret-key"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["connection_ids"] == ["reload-demo"]

    # The registry actually swapped — /api/v1/connections now sees it too.
    async with AsyncClient(transport=ASGITransport(app=reload_app), base_url=_BASE_URL) as client:
        listed = await client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer secret-key"}
        )
    assert [c["id"] for c in listed.json()] == ["reload-demo"]
