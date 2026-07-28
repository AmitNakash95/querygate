"""Integration tests for the catalog-governance admin API
(querygate/api/catalog_governance_routes.py) — full REST review/publish/
rollback (32B-1) and export/import/delete (32B-2) flows.
"""

from __future__ import annotations

from pathlib import Path

import yaml
import sqlalchemy as sa
import pytest
from httpx import ASGITransport, AsyncClient

from querygate.api.app import create_app
from querygate.catalog.loader import CatalogStore, set_catalog_store
from querygate.catalog.models import (
    CatalogDraftObjectType,
    CatalogDraftTarget,
    CatalogUsageSignalKind,
)
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.catalog.usage import build_usage_signal
from querygate.core.config import AppConfig

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_ADMIN_KEY = "catalog-governance-admin-key"
_ALL_CATALOG_SCOPES = [
    "catalog:generate",
    "catalog:author",
    "catalog:review",
    "catalog:edit",
    "catalog:approve",
    "catalog:reject",
    "catalog:publish",
    "catalog:rollback",
    "catalog:export",
    "catalog:delete",
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
    connections_file.write_text("""
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${CATALOG_GOV_TEST_DB_URL}
    known_tables: [customers]
""")
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
        # Provenance fields the admin UI's catalog workspace (TODO.md item
        # 38) filters and displays alongside review_status/schema_status.
        assert proposals[0]["source_class"] == "inferred"
        assert proposals[0]["confidence"] == 0.8

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


@pytest.mark.asyncio
async def test_export_import_round_trip_via_rest(sources):
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
        proposal_id = (
            await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        ).json()[0]["proposal_id"]
        await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/approve", headers=_auth()
        )
        await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/publish", headers=_auth()
        )

        export_resp = await client.get("/api/v1/admin/catalog/demo/export", headers=_auth())
        assert export_resp.status_code == 200
        bundle = export_resp.json()
        assert bundle["connection_id"] == "demo"
        assert len(bundle["draft_proposals"]) == 1
        # The exported bundle must round-trip: no computed field (e.g.
        # provenance precedence) leaks into the response and blocks re-import.
        assert "precedence" not in str(bundle)

        import_resp = await client.post(
            "/api/v1/admin/catalog/demo/import", json=bundle, headers=_auth()
        )
        assert import_resp.status_code == 200
        assert import_resp.json()["outcome"] == "imported"

        after_import = await client.get(
            "/api/v1/demo/catalog/search", params={"q": "customer master"}, headers=_auth()
        )
        assert any(hit["table"] == "customers" for hit in after_import.json()["results"])


@pytest.mark.asyncio
async def test_import_requires_matching_connection_id_via_rest(sources):
    connections_file, policy_file, catalog_file, snapshot = sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        bundle = {
            "export_version": 1,
            "connection_id": "not-demo",
            "exported_at": "2026-01-01T00:00:00Z",
            "catalog_version": 2,
        }
        resp = await client.post("/api/v1/admin/catalog/demo/import", json=bundle, headers=_auth())
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_single_proposal_fetch_includes_review_history_but_list_does_not(sources):
    """TODO.md item 38 phase 2: the admin UI's detail panel needs the durable
    review trail (who edited/rejected/approved and when); the bulk list
    endpoint deliberately stays lean without it (an unbounded per-proposal
    history on every row of a proposal-queue scan is payload nobody needs)."""
    connections_file, policy_file, catalog_file, snapshot = sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        gen_resp = await client.post(
            "/api/v1/admin/catalog/demo/generate-drafts",
            json={"batch": _batch(snapshot, generation_id="gen-history")},
            headers=_auth(),
        )
        assert gen_resp.status_code == 201
        proposal_id = (
            await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        ).json()[0]["proposal_id"]

        await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/reject",
            json={"reason": "needs more evidence"},
            headers=_auth(),
        )

        detail_resp = await client.get(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}", headers=_auth()
        )
        assert detail_resp.status_code == 200
        history = detail_resp.json()["review_history"]
        assert len(history) == 1
        assert history[0]["action"] == "rejected"
        assert history[0]["reason"] == "needs more evidence"
        assert history[0]["actor"]

        list_resp = await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        assert "review_history" not in list_resp.json()[0]


@pytest.mark.asyncio
async def test_delete_and_bulk_delete_via_rest(sources):
    connections_file, policy_file, catalog_file, snapshot = sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        gen_resp = await client.post(
            "/api/v1/admin/catalog/demo/generate-drafts",
            json={"batch": _batch(snapshot, generation_id="gen-delete")},
            headers=_auth(),
        )
        assert gen_resp.status_code == 201
        proposal_id = (
            await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        ).json()[0]["proposal_id"]

        # A pending proposal cannot be deleted directly.
        early_delete = await client.delete(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}", headers=_auth()
        )
        assert early_delete.status_code == 409

        await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/reject",
            json={"reason": "cleanup"},
            headers=_auth(),
        )
        delete_resp = await client.delete(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}", headers=_auth()
        )
        assert delete_resp.status_code == 200
        assert delete_resp.json()["outcome"] == "deleted"

        list_resp = await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        assert list_resp.json() == []


# --- TODO item 32C: usage-based learning REST surface -----------------------


def _snapshot_with_orders() -> ObservedSchemaSnapshot:
    metadata = sa.MetaData()
    customers = sa.Table("customers", metadata, sa.Column("id", sa.Integer, primary_key=True))
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id")),
    )
    return ObservedSchemaSnapshot.from_tables("demo", [customers, orders])


def _relationship_target() -> CatalogDraftTarget:
    return CatalogDraftTarget(
        connection_id="demo",
        object_type=CatalogDraftObjectType.RELATIONSHIP,
        table="orders",
        column="customer_id",
        to_table="customers",
        to_column="id",
    )


def _write_source_files_with_usage_signals(tmp_path, *, support: int = 5):
    connections_file = tmp_path / "connections.yaml"
    connections_file.write_text("""
connections:
  - id: demo
    dialect: postgresql
    connection_string: ${CATALOG_GOV_TEST_DB_URL}
    known_tables: [customers, orders]
""")
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("default:\n  enabled: true\n")

    snapshot = _snapshot_with_orders()
    target = _relationship_target()
    signals = [
        build_usage_signal(
            connection_id="demo",
            principal_subject=f"user-{i}",
            target=target,
            kind=CatalogUsageSignalKind.RELATIONSHIP_USED,
            schema_fingerprint=snapshot.fingerprint,
            evidence_reference=f"admission:{i}",
        )
        for i in range(support)
    ]
    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "connections": {"demo": {"tables": {}}},
                "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
                "usage_signals": [
                    signal.model_dump(mode="json", exclude_none=True) for signal in signals
                ],
            },
            sort_keys=False,
        )
    )
    return str(connections_file), str(policy_file), str(catalog_file), snapshot


@pytest.fixture
def usage_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOG_GOV_TEST_DB_URL", "postgresql+asyncpg://user:pass@localhost/x")
    return _write_source_files_with_usage_signals(tmp_path)


@pytest.mark.asyncio
async def test_learn_endpoint_generates_and_can_be_approved_and_published(usage_sources):
    connections_file, policy_file, catalog_file, _snapshot = usage_sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        learn_resp = await client.post("/api/v1/admin/catalog/demo/learn", headers=_auth())
        assert learn_resp.status_code == 201
        assert learn_resp.json()["outcome"] == "generated"
        assert learn_resp.json()["added_proposal_count"] == 1

        list_resp = await client.get("/api/v1/admin/catalog/demo/proposals", headers=_auth())
        proposals = list_resp.json()
        assert len(proposals) == 1
        proposal_id = proposals[0]["proposal_id"]
        assert proposals[0]["review_status"] == "pending"

        # A learned proposal cannot publish itself.
        early_publish = await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/publish", headers=_auth()
        )
        assert early_publish.status_code == 409

        approve_resp = await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/approve", headers=_auth()
        )
        assert approve_resp.status_code == 200
        publish_resp = await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/publish", headers=_auth()
        )
        assert publish_resp.status_code == 200

        # A second /learn call against the same evidence must not resurrect
        # a duplicate proposal for the now-published relationship.
        second_learn = await client.post("/api/v1/admin/catalog/demo/learn", headers=_auth())
        assert second_learn.status_code == 201
        assert second_learn.json()["added_proposal_count"] == 0


@pytest.mark.asyncio
async def test_learn_requires_generate_scope(usage_sources):
    connections_file, policy_file, catalog_file, _snapshot = usage_sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=["catalog:review"])
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/admin/catalog/demo/learn", headers=_auth())
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_learn_unknown_connection_returns_404(usage_sources):
    connections_file, policy_file, catalog_file, _snapshot = usage_sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/admin/catalog/does-not-exist/learn", headers=_auth())
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_usage_signals_endpoint_reports_aggregated_support(usage_sources):
    connections_file, policy_file, catalog_file, _snapshot = usage_sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    # get_catalog_store() only lazy-loads once per process; the autouse
    # conftest fixture already reset it to an empty store this test, so a
    # fresh app must be told to read this test's own catalog file, exactly
    # like conftest's own use of set_catalog_store (a "tests and
    # programmatic setup" API per catalog/loader.py's docstring).
    set_catalog_store(CatalogStore.from_file(catalog_file))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/admin/catalog/demo/usage-signals", headers=_auth())
        assert resp.status_code == 200
        summaries = resp.json()
        assert len(summaries) == 1
        assert summaries[0]["kind"] == "relationship_used"
        assert summaries[0]["support"] == 5
        assert summaries[0]["table"] == "orders"
        assert summaries[0]["to_table"] == "customers"


@pytest.mark.asyncio
async def test_usage_signals_endpoint_requires_review_scope(usage_sources):
    connections_file, policy_file, catalog_file, _snapshot = usage_sources
    app = create_app(_settings(connections_file, policy_file, catalog_file, scopes=[]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/admin/catalog/demo/usage-signals", headers=_auth())
        assert resp.status_code == 403


# --- Manual authoring (TODO item 84) ---------------------------------------


def _manual_body() -> dict:
    return {
        "object_type": "column",
        "table": "customers",
        "column": "email",
        "description": "Primary contact email.",
        "aliases": ["contact_email"],
    }


@pytest.mark.asyncio
async def test_manual_create_requires_author_scope(sources):
    connections_file, policy_file, catalog_file, _snapshot = sources

    # A reviewer without catalog:author cannot author a manual entry.
    reviewer_app = create_app(
        _settings(
            connections_file,
            policy_file,
            catalog_file,
            scopes=["catalog:review", "catalog:approve", "catalog:publish"],
        )
    )
    async with AsyncClient(transport=ASGITransport(app=reviewer_app), base_url=_BASE_URL) as client:
        denied = await client.post(
            "/api/v1/admin/catalog/demo/proposals", json=_manual_body(), headers=_auth()
        )
        assert denied.status_code == 403

    # catalog:author alone can create a quarantined manual proposal.
    author_app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=["catalog:author"])
    )
    async with AsyncClient(transport=ASGITransport(app=author_app), base_url=_BASE_URL) as client:
        created = await client.post(
            "/api/v1/admin/catalog/demo/proposals", json=_manual_body(), headers=_auth()
        )
        assert created.status_code == 201
        assert created.json()["outcome"] == "manual_created"


@pytest.mark.asyncio
async def test_manual_author_holding_review_can_self_approve_and_publish(sources):
    connections_file, policy_file, catalog_file, _snapshot = sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=_ALL_CATALOG_SCOPES)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        created = await client.post(
            "/api/v1/admin/catalog/demo/proposals", json=_manual_body(), headers=_auth()
        )
        assert created.status_code == 201
        proposal_id = created.json()["proposal_id"]

        # It surfaces in the existing review queue tagged as a manual source.
        listed = await client.get(
            "/api/v1/admin/catalog/demo/proposals",
            params={"review_status": "pending"},
            headers=_auth(),
        )
        manual = [p for p in listed.json() if p["proposal_id"] == proposal_id]
        assert manual and manual[0]["source_class"] == "manual"

        # Separation of duties is scope-based, not identity-based: the same
        # principal that authored it (and also holds catalog:review) may
        # approve and publish its own manual proposal — allowed, not blocked.
        approve = await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/approve", headers=_auth()
        )
        assert approve.status_code == 200
        publish = await client.post(
            f"/api/v1/admin/catalog/demo/proposals/{proposal_id}/publish", headers=_auth()
        )
        assert publish.status_code == 200

        # It lands as a verified entry in the live CATALOG_FILE via the
        # governance path (CatalogFileRepository) — never a second store.
        catalog = yaml.safe_load(Path(catalog_file).read_text())
        column = catalog["connections"]["demo"]["tables"]["customers"]["columns"]["email"]
        assert column["description"] == "Primary contact email."
        assert column["provenance"]["source_class"] == "verified"
        assert column["provenance"]["approved_by"] is not None

        # No ConfigVersionStore catalog snapshot path was introduced: the only
        # catalog file that changed is CATALOG_FILE itself.
        assert not list(Path(catalog_file).parent.glob("config_versions/**/catalog.yaml"))


@pytest.mark.asyncio
async def test_manual_proposal_target_absent_is_rejected(sources):
    connections_file, policy_file, catalog_file, _snapshot = sources
    app = create_app(
        _settings(connections_file, policy_file, catalog_file, scopes=["catalog:author"])
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        body = {"object_type": "table", "table": "ghost", "description": "Nope."}
        resp = await client.post("/api/v1/admin/catalog/demo/proposals", json=body, headers=_auth())
        assert resp.status_code == 409
