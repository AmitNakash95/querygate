"""Integration tests for the admin connection-status API (item 43 phase 1)
— GET /api/v1/admin/connections."""

from __future__ import annotations

import time

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.connections.engine import get_metadata
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.health import ConnectionHealth, HealthMonitor

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "conn-admin-key"
# A raw driver error that embeds host/credentials — must never reach the API.
_LEAKY_ERROR = "asyncpg: could not connect to db.internal:5432 user=svc password=hunter2"


def _settings(scopes) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        api_keys=[_ADMIN_KEY],
        api_key_scopes=list(scopes),
    )


def _registry() -> ConnectionRegistry:
    return ConnectionRegistry(
        {
            "demo": ConnectionProfile(
                id="demo", dialect="postgresql", connection_string="postgresql+asyncpg://u:p@h/demo"
            ),
            "reporting": ConnectionProfile(
                id="reporting", dialect="mssql", connection_string="mssql+aioodbc://u:p@h/rep"
            ),
            "legacy": ConnectionProfile(
                id="legacy",
                dialect="postgresql",
                connection_string="postgresql+asyncpg://u:p@h/legacy",
                enabled=False,
            ),
        }
    )


def _app_with_seeded_health(scopes=("admin:connections:read",)):
    set_registry(_registry())
    # Make 'demo' look schema-reflected by putting a table in its metadata cache.
    sa.Table("orders", get_metadata("demo"))
    app = create_app(_settings(scopes))
    monitor = HealthMonitor(interval_seconds=1000)
    now = time.time()
    monitor._status = {
        "demo": ConnectionHealth(
            connection_id="demo",
            healthy=True,
            last_checked=now,
            last_success=now,
            latency_ms=1.25,
        ),
        "reporting": ConnectionHealth(
            connection_id="reporting",
            healthy=False,
            last_checked=now,
            last_success=now - 300,
            latency_ms=2.5,
            failure_category="unreachable",
            error=_LEAKY_ERROR,
        ),
    }
    app.state.health_monitor = monitor
    return app


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_lists_credential_free_per_connection_status():
    app = _app_with_seeded_health()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/admin/connections", headers=_auth(_ADMIN_KEY))

    assert resp.status_code == 200
    by_id = {c["connection_id"]: c for c in resp.json()}

    assert by_id["demo"]["status"] == "healthy"
    assert by_id["demo"]["dialect"] == "postgresql"
    assert by_id["demo"]["latency_ms"] == 1.25
    assert by_id["demo"]["failure_category"] is None
    assert by_id["demo"]["schema_reflected"] is True

    assert by_id["reporting"]["status"] == "degraded"
    assert by_id["reporting"]["failure_category"] == "unreachable"
    assert by_id["reporting"]["last_success"] is not None
    assert by_id["reporting"]["schema_reflected"] is False

    # Deployment-disabled connections are reported as disabled with no probe data.
    assert by_id["legacy"]["status"] == "disabled"
    assert by_id["legacy"]["last_checked"] is None


@pytest.mark.asyncio
async def test_response_is_sorted_and_never_leaks_raw_error_or_credentials():
    app = _app_with_seeded_health()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/admin/connections", headers=_auth(_ADMIN_KEY))

    ids = [c["connection_id"] for c in resp.json()]
    assert ids == sorted(ids) == ["demo", "legacy", "reporting"]
    text = resp.text
    # No raw driver error, host, credential, or connection string anywhere.
    for leak in ("db.internal", "hunter2", "password", "asyncpg://", "aioodbc://"):
        assert leak not in text


@pytest.mark.asyncio
async def test_unknown_when_health_monitor_absent():
    # Route stays robust (no 500) if the monitor isn't wired yet.
    set_registry(_registry())
    app = create_app(_settings(("admin:connections:read",)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/admin/connections", headers=_auth(_ADMIN_KEY))
    assert resp.status_code == 200
    by_id = {c["connection_id"]: c for c in resp.json()}
    assert by_id["demo"]["status"] == "unknown"
    assert by_id["legacy"]["status"] == "disabled"
