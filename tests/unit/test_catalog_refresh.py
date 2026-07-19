"""Automatic row-free refresh, selective staleness, and persistence tests."""

from __future__ import annotations

import json
import os
import stat
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa

from querygate.catalog.generation import generate_catalog_drafts
from querygate.catalog.loader import CatalogStore
from querygate.catalog.providers import (
    ManualDraftBatch,
    ManualSemanticMemoryProvider,
    SemanticGenerationRequest,
)
from querygate.catalog.refresh import refresh_catalog_schema
from querygate.catalog.refresh import CatalogRefreshMonitor
from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate
from querygate.catalog.schema_memory import ObservedSchemaSnapshot


def _snapshot(*, email_type=sa.String(200), remove_orders: bool = False):
    metadata = sa.MetaData()
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", email_type),
    )
    tables = [customers]
    if not remove_orders:
        orders = sa.Table(
            "orders",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id")),
        )
        tables.append(orders)
    return ObservedSchemaSnapshot.from_tables("demo", tables)


def _store(snapshot: ObservedSchemaSnapshot) -> CatalogStore:
    return CatalogStore.from_dict(
        {
            "version": 2,
            "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
            "connections": {
                "demo": {
                    "tables": {
                        "customers": {
                            "description": "Customer accounts.",
                            "columns": {
                                "id": {"description": "Stable customer id."},
                                "email": {"description": "Contact address."},
                            },
                        },
                        "orders": {
                            "description": "Customer orders.",
                            "relationships": [
                                {
                                    "column": "customer_id",
                                    "to_table": "customers",
                                    "to_column": "email",
                                    "description": "Deliberately email-dependent test hint.",
                                }
                            ],
                        },
                    }
                }
            },
        }
    )


def _with_drafts(store: CatalogStore) -> CatalogStore:
    snapshot = store.get_schema_snapshot("demo")
    batch = ManualDraftBatch.model_validate(
        {
            "generation_id": "refresh-test",
            "connection_id": "demo",
            "schema_fingerprint": snapshot.fingerprint,
            "suggestions": [
                {
                    "target": {
                        "connection_id": "demo",
                        "object_type": "column",
                        "table": "customers",
                        "column": "email",
                    },
                    "content": {"description": "Email proposal."},
                    "confidence": 0.6,
                },
                {
                    "target": {
                        "connection_id": "demo",
                        "object_type": "column",
                        "table": "customers",
                        "column": "id",
                    },
                    "content": {"description": "ID proposal."},
                    "confidence": 0.7,
                },
            ],
        }
    )
    return generate_catalog_drafts(
        store,
        request=SemanticGenerationRequest(
            generation_id="refresh-test", connection_id="demo", snapshot=snapshot
        ),
        provider=ManualSemanticMemoryProvider(batch),
    ).store


def test_refresh_marks_only_changed_entries_and_dependent_relationships_stale():
    before = _snapshot()
    store = _with_drafts(_store(before))
    after = _snapshot(email_type=sa.Text)

    update = refresh_catalog_schema(store, after)

    customers = update.store.get_table("demo", "customers")
    orders = update.store.get_table("demo", "orders")
    assert customers.provenance.status == "verified"
    assert customers.provenance.schema_fingerprint == after.fingerprint
    assert customers.column("id").provenance.status == "verified"
    assert customers.column("id").provenance.schema_fingerprint == after.fingerprint
    assert customers.column("email").provenance.status == "stale"
    assert customers.column("email").provenance.schema_fingerprint == before.fingerprint
    assert orders.provenance.status == "verified"
    assert orders.relationships[0].provenance.status == "stale"

    drafts = {draft.target.column: draft for draft in update.store.iter_draft_proposals()}
    assert drafts["email"].provenance.status == "stale"
    assert drafts["id"].provenance.status == "draft"
    assert drafts["id"].provenance.schema_fingerprint == after.fingerprint
    assert set(update.stale_entry_ids) >= {
        customers.column("email").provenance.entry_id,
        orders.relationships[0].provenance.entry_id,
        drafts["email"].proposal_id,
    }


def test_first_refresh_binds_untracked_entries_without_marking_them_stale():
    store = CatalogStore.from_dict(
        {"version": 2, "connections": {"demo": {"tables": {"customers": {}}}}}
    )
    snapshot = _snapshot()

    update = refresh_catalog_schema(store, snapshot)

    entry = update.store.get_table("demo", "customers")
    assert update.before_fingerprint is None
    assert update.stale_entry_ids == ()
    assert entry.provenance.status == "verified"
    assert entry.provenance.schema_fingerprint == snapshot.fingerprint


def test_first_refresh_upgrades_a_legacy_catalog_to_version_2():
    store = CatalogStore.from_dict(
        {"version": 1, "connections": {"demo": {"tables": {"customers": {}}}}}
    )

    update = refresh_catalog_schema(store, _snapshot())

    assert update.store.version == 2
    assert update.store.get_table("demo", "customers").provenance.catalog_version == 2


def test_refresh_is_idempotent_for_the_same_snapshot():
    snapshot = _snapshot()
    store = _store(snapshot)

    update = refresh_catalog_schema(store, snapshot)

    assert not update.changed
    assert update.store is store
    assert update.diff is not None
    assert not update.diff.changed


def test_removed_table_stales_only_that_table_tree():
    before = _snapshot()
    update = refresh_catalog_schema(_store(before), _snapshot(remove_orders=True))

    assert update.store.get_table("demo", "orders").provenance.status == "stale"
    assert update.store.get_table("demo", "customers").provenance.status == "verified"


def test_catalog_file_repository_writes_atomically_and_preserves_mode(tmp_path):
    snapshot = _snapshot()
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text("version: 2\nconnections:\n  demo:\n    tables:\n      customers: {}\n")
    os.chmod(catalog_path, 0o640)
    repository = CatalogFileRepository(str(catalog_path))

    def _apply(store):
        update = refresh_catalog_schema(store, snapshot)
        return CatalogFileUpdate(update.store, update)

    first = repository.update(_apply)
    second = repository.update(_apply)
    reloaded = CatalogStore.from_file(str(catalog_path))

    assert first.changed
    assert not second.changed
    assert reloaded.get_schema_snapshot("demo").fingerprint == snapshot.fingerprint
    assert stat.S_IMODE(catalog_path.stat().st_mode) == 0o640
    assert "postgresql" not in json.dumps(reloaded.to_dict())


@pytest.mark.asyncio
async def test_refresh_monitor_scans_and_persists_outside_the_query_path(tmp_path):
    snapshot = _snapshot()
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text("version: 2\nconnections:\n  demo:\n    tables:\n      customers: {}\n")
    monitor = CatalogRefreshMonitor(
        catalog_file=str(catalog_path), interval_seconds=60, max_tables=10
    )

    with patch(
        "querygate.catalog.refresh.scan_connection_schema",
        new=AsyncMock(return_value=snapshot),
    ) as scan:
        update = await monitor.refresh_once("demo")

    scan.assert_awaited_once_with("demo", max_tables=10)
    assert update.changed
    assert CatalogStore.from_file(str(catalog_path)).get_schema_snapshot("demo") is not None


@pytest.mark.asyncio
async def test_schema_scan_enforces_table_bound_before_reflection():
    from querygate.catalog.refresh import scan_connection_schema

    registry = MagicMock()
    registry.get.return_value = MagicMock(known_tables=["incomplete_seed"])
    engine = MagicMock()
    with (
        patch("querygate.catalog.refresh.get_registry", return_value=registry),
        patch(
            "querygate.catalog.refresh.list_live_tables",
            new=AsyncMock(return_value=[f"table_{index}" for index in range(11)]),
        ) as live_tables,
        patch("querygate.catalog.refresh.get_engine", return_value=engine) as get_engine_mock,
    ):
        with pytest.raises(ValueError, match="table limit"):
            await scan_connection_schema("demo", max_tables=10)

    live_tables.assert_awaited_once_with("demo")
    get_engine_mock.assert_not_called()


@pytest.mark.asyncio
async def test_async_catalog_lock_orders_scan_diff_write_transactions(tmp_path):
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text("version: 2\nconnections:\n  demo:\n    tables:\n      customers: {}\n")
    first_repository = CatalogFileRepository(str(catalog_path))
    second_repository = CatalogFileRepository(str(catalog_path))
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    first_snapshot = _snapshot()
    second_snapshot = _snapshot(email_type=sa.Text)

    async def _first(store):
        first_entered.set()
        await release_first.wait()
        update = refresh_catalog_schema(store, first_snapshot)
        return CatalogFileUpdate(update.store, update)

    async def _second(store):
        second_entered.set()
        update = refresh_catalog_schema(store, second_snapshot)
        return CatalogFileUpdate(update.store, update)

    first_task = asyncio.create_task(first_repository.update_async(_first))
    await first_entered.wait()
    second_task = asyncio.create_task(second_repository.update_async(_second))
    await asyncio.sleep(0.02)
    assert not second_entered.is_set()

    release_first.set()
    first_update, second_update = await asyncio.gather(first_task, second_task)

    assert first_update.before_fingerprint is None
    assert second_update.before_fingerprint == first_snapshot.fingerprint
    persisted = CatalogStore.from_file(str(catalog_path)).get_schema_snapshot("demo")
    assert persisted.fingerprint == second_snapshot.fingerprint
