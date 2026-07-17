"""Integration tests for the REST API surface."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig
from querygate.execution.service import (
    BatchQueryItemResult,
    ColumnInfo,
    StructuredQueryResult,
    TableDescription,
)

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_SERVICE = "querygate.api.routes.StructuredQueryService"


def _settings(**overrides) -> AppConfig:
    base = dict(environment="localhost", mcp_enabled=False)
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
        side_effect=ValueError("Column 'x' not found"),
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
async def test_health_endpoint(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "querygate"
