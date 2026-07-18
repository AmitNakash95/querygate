"""Integration tests for the config-governance admin API
(querygate/api/admin_config_routes.py) — full REST stage/apply/rollback flow.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
# One key with both scopes — read/write scope *separation* is a security
# boundary concern, covered with dedicated single-scope principals in
# tests/security/test_adversarial_security.py, not here. This file exercises
# the REST flow's functional correctness only.
_ADMIN_KEY = "governance-admin-key"


def _write_source_files(tmp_path):
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text(
        """
connections:
  - id: gov-demo
    dialect: postgresql
    connection_string: ${GOV_TEST_DB_URL}
    known_tables: [foo]
"""
    )
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("default:\n  enabled: true\n")
    return str(connections_file), str(policy_file)


def _settings(connections_file: str, policy_file: str) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        connections_file=connections_file,
        policy_file=policy_file,
        api_keys=[_ADMIN_KEY],
        api_key_scopes=["admin:config:write", "admin:config:read"],
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("GOV_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    return create_app(_settings(connections_file, policy_file))


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_current_bootstraps_version_1(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/admin/config/current", headers=_auth(_ADMIN_KEY))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "1"
    assert body["status"] == "active"
    assert body["created_by"] == "system:bootstrap"


@pytest.mark.asyncio
async def test_validate_endpoint_reports_errors_without_persisting(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/validate",
            json={"connections_yaml": "connections:\n  - id: bad\n    dialect: postgresql\n"},
            headers=_auth(_ADMIN_KEY),
        )
        versions_resp = await client.get("/api/v1/admin/config/versions", headers=_auth(_ADMIN_KEY))

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["errors"]
    assert len(versions_resp.json()) == 1  # only the bootstrap version


@pytest.mark.asyncio
async def test_preview_endpoint_returns_redacted_document_diff_without_persisting(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/preview",
            json={"policy_yaml": "default:\n  enabled: true\n  max_joins: 2\n"},
            headers=_auth(_ADMIN_KEY),
        )
        versions_resp = await client.get("/api/v1/admin/config/versions", headers=_auth(_ADMIN_KEY))

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["ready_to_stage"] is True
    assert {item["document"]: item["change"] for item in body["documents"]} == {
        "connections": "unchanged",
        "policy": "changed",
        "catalog": "unchanged",
    }
    assert "max_joins" not in resp.text
    assert len(versions_resp.json()) == 1


@pytest.mark.asyncio
async def test_full_stage_apply_rollback_flow(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        stage_resp = await client.post(
            "/api/v1/admin/config/versions",
            json={
                "policy_yaml": "default:\n  enabled: true\n  max_joins: 2\n",
                "description": "tighten max_joins for a pilot customer",
            },
            headers=_auth(_ADMIN_KEY),
        )
        assert stage_resp.status_code == 201
        staged = stage_resp.json()
        assert staged["status"] == "staged"
        assert staged["id"] == "2"

        list_resp = await client.get("/api/v1/admin/config/versions", headers=_auth(_ADMIN_KEY))
        assert [v["id"] for v in list_resp.json()] == ["1", "2"]

        get_resp = await client.get(
            f"/api/v1/admin/config/versions/{staged['id']}", headers=_auth(_ADMIN_KEY)
        )
        assert get_resp.status_code == 200
        assert "max_joins: 2" in get_resp.json()["policy_yaml"]

        apply_resp = await client.post(
            f"/api/v1/admin/config/versions/{staged['id']}/apply", headers=_auth(_ADMIN_KEY)
        )
        assert apply_resp.status_code == 200
        applied = apply_resp.json()
        assert applied["version"]["status"] == "active"
        assert applied["version"]["previous_active_version_id"] == "1"
        assert applied["reload"]["connection_ids"] == ["gov-demo"]

        current_resp = await client.get("/api/v1/admin/config/current", headers=_auth(_ADMIN_KEY))
        assert current_resp.json()["id"] == "2"

        # Roll back — same endpoint, targeting the version that was active before.
        rollback_resp = await client.post(
            "/api/v1/admin/config/versions/1/apply", headers=_auth(_ADMIN_KEY)
        )
        assert rollback_resp.status_code == 200
        assert rollback_resp.json()["version"]["id"] == "1"
        assert rollback_resp.json()["version"]["status"] == "active"

        current_after_rollback = await client.get(
            "/api/v1/admin/config/current", headers=_auth(_ADMIN_KEY)
        )
        assert current_after_rollback.json()["id"] == "1"


@pytest.mark.asyncio
async def test_stage_with_invalid_content_returns_422(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/versions",
            json={"connections_yaml": "not-a-valid: [connections shape"},
            headers=_auth(_ADMIN_KEY),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_apply_unknown_version_returns_404(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/versions/does-not-exist/apply", headers=_auth(_ADMIN_KEY)
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_unknown_version_returns_404(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get(
            "/api/v1/admin/config/versions/does-not-exist", headers=_auth(_ADMIN_KEY)
        )
    assert resp.status_code == 404
