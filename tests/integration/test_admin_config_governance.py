"""Integration tests for the config-governance admin API
(querygate/api/admin_config_routes.py) — full REST stage/apply/rollback flow.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.admin.store import get_config_version_store
from querygate.api.app import create_app
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.policy.loader import PolicyStore, set_policy_store

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
async def test_candidate_simulation_uses_draft_without_persisting_or_changing_live_policy(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GOV_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    source_connections = Path(connections_file).read_text()
    source_policy = Path(policy_file).read_text()
    set_registry(ConnectionRegistry.from_file(connections_file))
    set_policy_store(PolicyStore.from_file(policy_file))
    candidate_policy = """
default:
  enabled: true
  denied_tables: [foo]
  mandatory_row_filters:
    - table: foo
      column: hidden_tenant_id
      value: static-secret-must-not-return
"""
    app = create_app(_settings(connections_file, policy_file))
    candidate_request = {
        "policy_yaml": candidate_policy,
        "principal": "reporting-agent",
        "connection": "gov-demo",
        "table": "foo",
        "columns": ["id"],
        "claims": {"tenant_id": "claim-secret-must-not-return"},
        "query": {
            "from": "foo",
            "select": ["foo.id"],
            "where": {"col": "foo.id", "op": "eq", "value": "literal-must-not-return"},
        },
    }
    active_request = {
        "principal": "reporting-agent",
        "connection": "gov-demo",
        "table": "foo",
        "columns": ["id"],
        "claims": {},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        calls = []
        for _ in range(12):
            calls.extend(
                [
                    client.post(
                        "/api/v1/admin/config/simulate",
                        json=candidate_request,
                        headers=_auth(_ADMIN_KEY),
                    ),
                    client.post(
                        "/api/v1/admin/ui/policy/test",
                        json=active_request,
                        headers=_auth(_ADMIN_KEY),
                    ),
                ]
            )
        responses = await asyncio.gather(*calls)

    candidate_responses = responses[0::2]
    active_responses = responses[1::2]
    assert all(response.status_code == 200 for response in candidate_responses)
    assert all(response.json()["decision"] == "deny" for response in candidate_responses)
    assert all(response.status_code == 200 for response in active_responses)
    assert all(response.json()["allowed"] is True for response in active_responses)
    candidate_text = "".join(response.text for response in candidate_responses)
    assert "static-secret-must-not-return" not in candidate_text
    assert "claim-secret-must-not-return" not in candidate_text
    assert "literal-must-not-return" not in candidate_text
    assert "hidden_tenant_id" not in candidate_text
    assert get_config_version_store().list_versions() == []
    assert Path(connections_file).read_text() == source_connections
    assert Path(policy_file).read_text() == source_policy


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
async def test_diff_endpoint_reports_semantic_changes_without_persisting(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/diff",
            # Active default max_joins is 5; tighten it and add a mandatory filter.
            json={
                "policy_yaml": (
                    "default:\n"
                    "  enabled: true\n"
                    "  max_joins: 2\n"
                    "  mandatory_row_filters:\n"
                    "    - table: foo\n"
                    "      column: tenant_id\n"
                    "      value: 99\n"
                )
            },
            headers=_auth(_ADMIN_KEY),
        )
        versions_resp = await client.get("/api/v1/admin/config/versions", headers=_auth(_ADMIN_KEY))

    assert resp.status_code == 200
    body = resp.json()
    assert body["evaluation_scope"] == "connection_baseline"
    kinds = {(c["category"], c["object"], c["direction"]) for c in body["changes"]}
    assert ("guardrail", "max_joins", "tightening") in kinds
    assert ("mandatory_filter", "foo.tenant_id", "tightening") in kinds
    # The static filter value must never appear in the response.
    assert "99" not in resp.text
    # Diff persists nothing: only the bootstrapped version 1 exists.
    assert [v["id"] for v in versions_resp.json()] == ["1"]


@pytest.mark.asyncio
async def test_blast_radius_endpoint_finds_targeted_expansion_under_an_overall_tightening(app):
    # The default cap tightens for everyone (100 -> 50), but agent-a's own
    # override raises its effective cap to 5000 — a targeted expansion hidden
    # inside what looks, at the baseline, like a fleet-wide tightening.
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/blast-radius",
            json={
                "policy_yaml": (
                    "default:\n"
                    "  enabled: true\n"
                    "  max_limit: 50\n"
                    "principals:\n"
                    "  agent-a:\n"
                    "    '*':\n"
                    "      max_limit: 5000\n"
                )
            },
            headers=_auth(_ADMIN_KEY),
        )
        versions_resp = await client.get("/api/v1/admin/config/versions", headers=_auth(_ADMIN_KEY))

    assert resp.status_code == 200
    body = resp.json()
    assert body["evaluation_scope"] == "connection_baseline_plus_configured_principals"
    baseline_change = next(c for c in body["baseline"]["changes"] if c["object"] == "max_limit")
    assert baseline_change["direction"] == "tightening"
    assert body["principals_configured"] == 1
    assert body["principals_affected"] == 1
    (impact,) = body["principal_impacts"]
    assert impact["principal"] == "agent-a"
    principal_change = next(c for c in impact["changes"] if c["object"] == "max_limit")
    assert principal_change["direction"] == "loosening"
    assert principal_change["before"] == "100" and principal_change["after"] == "5000"
    # The targeted expansion is the highest-risk finding, scoped to agent-a —
    # the baseline tightening is not a risk and is excluded from this view.
    assert body["highest_risk"]
    top = body["highest_risk"][0]
    assert top["scope"] == "principal" and top["principal"] == "agent-a"
    assert top["change"]["object"] == "max_limit"
    # Blast-radius analysis persists nothing: only the bootstrapped version exists.
    assert [v["id"] for v in versions_resp.json()] == ["1"]


@pytest.mark.asyncio
async def test_blast_radius_endpoint_with_no_candidate_changes_is_empty(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/blast-radius", json={}, headers=_auth(_ADMIN_KEY)
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline"]["changes"] == []
    assert body["principal_impacts"] == []
    assert body["principals_configured"] == 0


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


@pytest.mark.asyncio
async def test_list_templates_requires_read_scope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        unauthenticated = await client.get("/api/v1/admin/config/templates")
        authorized = await client.get("/api/v1/admin/config/templates", headers=_auth(_ADMIN_KEY))

    assert unauthenticated.status_code == 401
    assert authorized.status_code == 200
    ids = {template["id"] for template in authorized.json()}
    assert "reporting-only" in ids
    assert "tenant-isolated" in ids


@pytest.mark.asyncio
async def test_render_template_requires_write_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("GOV_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    connections_file, policy_file = _write_source_files(tmp_path)
    read_only_app = create_app(
        AppConfig(
            environment="localhost",
            mcp_enabled=False,
            audit_sink_backend="none",
            connections_file=connections_file,
            policy_file=policy_file,
            api_keys=["reader-only-key"],
            api_key_scopes=["admin:config:read"],
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=read_only_app), base_url=_BASE_URL
    ) as client:
        resp = await client.post(
            "/api/v1/admin/config/templates/render",
            json={"template_id": "deny-by-default", "params": {}},
            headers=_auth("reader-only-key"),
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_render_template_rejects_unknown_template_id(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/admin/config/templates/render",
            json={"template_id": "does-not-exist", "params": {}},
            headers=_auth(_ADMIN_KEY),
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_render_template_end_to_end_through_validate_and_stage(app):
    """TODO.md item 46: a rendered template is not a special code path — it
    is plain policy_yaml text that goes through the exact same /validate and
    /versions (stage) endpoints as a hand-edited draft.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        rendered = await client.post(
            "/api/v1/admin/config/templates/render",
            json={
                "template_id": "reporting-only",
                "params": {"connection": "gov-demo", "allowed_tables": ["foo"], "max_limit": 25},
            },
            headers=_auth(_ADMIN_KEY),
        )
        assert rendered.status_code == 200
        policy_yaml = rendered.json()["policy_yaml"]
        assert rendered.json()["rules"]

        validated = await client.post(
            "/api/v1/admin/config/validate",
            json={"policy_yaml": policy_yaml},
            headers=_auth(_ADMIN_KEY),
        )
        assert validated.status_code == 200
        assert validated.json()["valid"] is True

        staged = await client.post(
            "/api/v1/admin/config/versions",
            json={"policy_yaml": policy_yaml, "description": "apply reporting-only template"},
            headers=_auth(_ADMIN_KEY),
        )
        assert staged.status_code == 201
        assert staged.json()["status"] == "staged"
