"""Integration tests for the admin connection-operations API (item 43):
GET /api/v1/admin/connections (phase 1) and
POST /api/v1/admin/connections/{id}/test (phase 2a, the "test now" probe)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from querygate import health as health_module
from querygate.api.app import create_app
from querygate.audit.sinks import JsonlAuditSink, set_audit_sink
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


def _app_with_live_monitor(scopes=("admin:connections:read", "admin:connections:test")):
    """A `HealthMonitor` that hasn't been `.start()`ed — the "test now"
    endpoint's `manual_check` calls the same `_check_once` seam directly, so
    patching module-level `_ping` is enough to control probe outcomes."""
    set_registry(_registry())
    app = create_app(_settings(scopes))
    app.state.health_monitor = HealthMonitor(interval_seconds=1000)
    return app


@pytest.mark.asyncio
async def test_test_now_unknown_connection_returns_404():
    app = _app_with_live_monitor()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/admin/connections/ghost/test", headers=_auth(_ADMIN_KEY))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_now_disabled_connection_returns_409():
    app = _app_with_live_monitor()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/admin/connections/legacy/test", headers=_auth(_ADMIN_KEY))
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_test_now_health_monitor_absent_returns_503():
    set_registry(_registry())
    app = create_app(_settings(("admin:connections:test",)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/admin/connections/demo/test", headers=_auth(_ADMIN_KEY))
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_test_now_success_updates_status_and_never_leaks_raw_error():
    app = _app_with_live_monitor()
    with patch.object(
        health_module,
        "_ping",
        new_callable=AsyncMock,
        side_effect=ConnectionRefusedError(_LEAKY_ERROR),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/admin/connections/demo/test", headers=_auth(_ADMIN_KEY)
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["connection_id"] == "demo"
    assert body["status"] == "degraded"
    assert body["failure_category"] == "unreachable"
    text = resp.text
    for leak in ("db.internal", "hunter2", "password"):
        assert leak not in text

    # The passive GET list reflects the just-run manual probe too.
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        list_resp = await client.get("/api/v1/admin/connections", headers=_auth(_ADMIN_KEY))
    by_id = {c["connection_id"]: c for c in list_resp.json()}
    assert by_id["demo"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_test_now_is_rate_limited_per_connection():
    app = _app_with_live_monitor()
    with patch.object(health_module, "_ping", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            first = await client.post(
                "/api/v1/admin/connections/demo/test", headers=_auth(_ADMIN_KEY)
            )
            second = await client.post(
                "/api/v1/admin/connections/demo/test", headers=_auth(_ADMIN_KEY)
            )
    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_test_now_rate_limit_is_per_connection_not_global():
    app = _app_with_live_monitor()
    with patch.object(health_module, "_ping", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            demo_resp = await client.post(
                "/api/v1/admin/connections/demo/test", headers=_auth(_ADMIN_KEY)
            )
            reporting_resp = await client.post(
                "/api/v1/admin/connections/reporting/test", headers=_auth(_ADMIN_KEY)
            )
    assert demo_resp.status_code == 200
    assert reporting_resp.status_code == 200


@pytest.mark.asyncio
async def test_test_now_persists_audit_event_without_raw_error(tmp_path):
    import json

    app = _app_with_live_monitor()
    audit_path = tmp_path / "events.jsonl"
    set_audit_sink(JsonlAuditSink(str(audit_path)))
    with patch.object(
        health_module,
        "_ping",
        new_callable=AsyncMock,
        side_effect=ConnectionRefusedError(_LEAKY_ERROR),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
            resp = await client.post(
                "/api/v1/admin/connections/demo/test", headers=_auth(_ADMIN_KEY)
            )
    assert resp.status_code == 200

    lines = audit_path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "connection.probe"
    assert event["connection_id"] == "demo"
    assert event["outcome"] == "success"
    assert event["probe_healthy"] is False
    assert event["failure_category"] == "unreachable"
    text = audit_path.read_text()
    for leak in ("db.internal", "hunter2", "password"):
        assert leak not in text
