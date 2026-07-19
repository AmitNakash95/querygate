"""Integration tests for the catalog-governance admin API
(querygate/api/catalog_governance_routes.py) — full REST review/publish/
rollback flow (TODO.md item 32B-1).
"""

from __future__ import annotations

import yaml
import sqlalchemy as sa
import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "catalog-governance-admin-key"
_ALL_CATALOG_SCOPES = [
    "catalog:generate",
    "catalog:review",
    "catalog:edit",
    "catalog:approve",
    "catalog:reject",
    "catalog:publish",
    "catalog:rollback",
]


def _snapshot() -> ObservedSchemaSnapshot:
    metadata = sa.MetaData()
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    return ObservedSchemaSnapshot.from_tables("demo", [customers])


def _write_source_files(tmp_path):
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text(
        """
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${CATALOG_GOV_TEST_DB_URL}
    known_tables: [customers]
"""
    )
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("default:\n  enabled: true\n")

    snapshot = _snapshot()
    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "connections": {"demo": {"tables": {}}},
                "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
            },
            sort_keys=False,
        )
    )
    return str(connections_file), str(policy_file), str(catalog_file), snapshot


def _settings(connections_file, policy_file, catalog_file, *, scopes) -> AppConfig:
    return AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        connections_file=connections_file,
        policy_file=policy_file,
        catalog_file=catalog_file,
        api_keys=[_ADMIN_KEY],
        api_key_scopes=scopes,
    )


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOG_GOV_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    return _write_source_files(tmp_path)


def _auth() -> dict:
    return {"Authorization": f"Bearer {_ADMIN_KEY}"}


def _batch(snapshot: ObservedSchemaSnapshot, *, generation_id: str = "gen-1") -> dict:
    return {
        "generation_id": generation_id,
        "connection_id": "demo",
        "schema_fingerprint": snapshot.fingerprint,
        "created_by": "offline-operator",
        "suggestions": [
            {
                "target": {"connection_id": "demo", "object_type": "table", "table": "customers"},
                "content": {"description": "Customer master data.", "aliases": ["accounts"]},
                "confidence": 0.8,
            }
        ],
    }


@pytest.mark.asyncio
async def test_full_generate_approve_publish_rollback_flow(sources):
    connections_file, policy_file, catalog_file, snapshot = sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        gen_resp = await client.post(
            "/api/v1/admin/catalog/demo/generate-drafts",
            json={"batch": _batch(snapshot)},
            headers=_auth(),
        )
        assert gen_resp.status_code == 201
        assert gen_resp.json()["added_proposal_count"] == 1

        list_resp = await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        assert list_resp.status_code == 200
        proposals = list_resp.json()
        assert len(proposals) == 1
        proposal_id = proposals[0]["proposal_id"]
        assert proposals[0]["review_status"] == "pending"

        # A draft cannot publish itself: publishing before approval must fail.
        early_publish = await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/publish", headers=_auth()
        )
        assert early_publish.status_code == 409

        approve_resp = await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/approve", headers=_auth()
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["outcome"] == "approved"

        preview_resp = await client.get(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/preview",
            params={"principal_subject": "any-reviewer"},
            headers=_auth(),
        )
        assert preview_resp.status_code == 200
        assert preview_resp.json()["visible_to_principal"] is True
        assert preview_resp.json()["would_conflict"] is False

        publish_resp = await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/publish", headers=_auth()
        )
        assert publish_resp.status_code == 200
        version_id = publish_resp.json()["version_id"]
        assert version_id == "1"

        # Published content is now agent-visible through ordinary catalog search.
        search_resp = await client.get(
            "/api/v1/demo/catalog/search",
            params={"q": "customer master"},
            headers=_auth(),
        )
        assert search_resp.status_code == 200
        assert any(hit["table"] == "customers" for hit in search_resp.json()["results"])

        versions_resp = await client.get("/api/v1/admin/catalog/demo/versions", headers=_auth())
        assert versions_resp.status_code == 200
        assert len(versions_resp.json()) == 1
        assert versions_resp.json()[0]["action"] == "publish"
        # Metadata-only: field names, never their content.
        assert versions_resp.json()[0]["fields_changed"] == ["description", "aliases"]
        assert "Customer master data" not in str(versions_resp.json())

        rollback_resp = await client.post(
            f"/api/v1/admin/catalog/demo/versions/{version_id}/rollback", headers=_auth()
        )
        assert rollback_resp.status_code == 200

        search_after_rollback = await client.get(
            "/api/v1/demo/catalog/search",
            params={"q": "customer master"},
            headers=_auth(),
        )
        assert search_after_rollback.json()["results"] == []


@pytest.mark.asyncio
async def test_bulk_reject_endpoint_is_atomic(sources):
    connections_file, policy_file, catalog_file, snapshot = sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        gen_resp = await client.post(
            "/api/v1/admin/catalog/demo/generate-drafts",
            json={"batch": _batch(snapshot)},
            headers=_auth(),
        )
        assert gen_resp.status_code == 201
        list_resp = await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        real_id = list_resp.json()[0]["proposal_id"]

        bad_bulk = await client.post(
            "/api/v1/admin/catalog/demo/proposals/bulk-reject",
            json={"proposal_ids": [real_id, "does-not-exist"], "reason": "bad batch"},
            headers=_auth(),
        )
        assert bad_bulk.status_code == 404

        # No partial mutation from the failed bulk call.
        unaffected = await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        assert unaffected.json()[0]["review_status"] == "pending"

        good_bulk = await client.post(
            "/api/v1/admin/catalog/demo/proposals/bulk-reject",
            json={"proposal_ids": [real_id], "reason": "duplicate"},
            headers=_auth(),
        )
        assert good_bulk.status_code == 200
        assert good_bulk.json()["outcome"] == "bulk_rejected"


@pytest.mark.asyncio
async def test_unknown_connection_and_unknown_proposal_return_404(sources):
    connections_file, policy_file, catalog_file, _snapshot = sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        unknown_connection = await client.get(
            "/api/v1/admin/catalog/does-not-exist/proposals", headers=_auth()
        )
        assert unknown_connection.status_code == 404

        unknown_proposal = await client.get(
            "/api/v1/admin/catalog/demo/proposals/does-not-exist", headers=_auth()
        )
        assert unknown_proposal.status_code == 404
