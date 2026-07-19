"""TODO item 32C: usage-signal shape, idempotent recording, the in-process
buffer, the anti-feedback-loop gate, and the background learning monitor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa

from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogDraftObjectType,
    CatalogDraftTarget,
    CatalogEvidence,
    CatalogUsageSignal,
    CatalogUsageSignalKind,
)
from querygate.catalog.repository import CatalogFileRepository
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.catalog.usage import (
    CatalogUsageLearningMonitor,
    InProcessUsageSignalBuffer,
    build_usage_signal,
    hash_principal_partition,
    record_usage_signals,
    should_emit_signal,
    summarize_usage_signals,
)
import querygate.catalog.usage as usage_module


def _relationship_target(**overrides) -> CatalogDraftTarget:
    defaults = dict(
        connection_id="demo",
        object_type=CatalogDraftObjectType.RELATIONSHIP,
        table="orders",
        column="customer_id",
        to_table="customers",
        to_column="id",
    )
    defaults.update(overrides)
    return CatalogDraftTarget(**defaults)


def _table_target(**overrides) -> CatalogDraftTarget:
    defaults = dict(connection_id="demo", object_type=CatalogDraftObjectType.TABLE, table="orders")
    defaults.update(overrides)
    return CatalogDraftTarget(**defaults)


def _signal(*, principal="user-1", evidence_ref="admission:1", target=None, kind=None, **overrides):
    target = target or _relationship_target()
    kind = kind or CatalogUsageSignalKind.RELATIONSHIP_USED
    return build_usage_signal(
        connection_id=overrides.pop("connection_id", "demo"),
        principal_subject=principal,
        target=target,
        kind=kind,
        schema_fingerprint=overrides.pop("schema_fingerprint", "fp-1"),
        evidence_reference=evidence_ref,
        observed_at=overrides.pop("observed_at", None),
    )


def _snapshot() -> ObservedSchemaSnapshot:
    metadata = sa.MetaData()
    customers = sa.Table("customers", metadata, sa.Column("id", sa.Integer, primary_key=True))
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id")),
    )
    return ObservedSchemaSnapshot.from_tables("demo", [customers, orders])


# --- shape/validation -------------------------------------------------------


def test_signal_kind_must_match_target_object_type():
    with pytest.raises(Exception):
        build_usage_signal(
            connection_id="demo",
            principal_subject="user-1",
            target=_table_target(),
            kind=CatalogUsageSignalKind.RELATIONSHIP_USED,
            schema_fingerprint="fp-1",
            evidence_reference="admission:1",
        )


def test_signal_target_connection_must_match_signal_connection():
    with pytest.raises(Exception):
        CatalogUsageSignal(
            signal_id="urn:querygate:catalog:usage:deadbeef",
            connection_id="demo",
            principal_partition=hash_principal_partition("user-1"),
            target=_relationship_target(connection_id="other"),
            kind=CatalogUsageSignalKind.RELATIONSHIP_USED,
            schema_fingerprint="fp-1",
            observed_at=datetime.now(timezone.utc),
            evidence=CatalogEvidence(kind="usage", reference="admission:1"),
        )


def test_evidence_must_use_usage_kind():
    with pytest.raises(Exception):
        CatalogUsageSignal(
            signal_id="urn:querygate:catalog:usage:deadbeef",
            connection_id="demo",
            principal_partition=hash_principal_partition("user-1"),
            target=_relationship_target(),
            kind=CatalogUsageSignalKind.RELATIONSHIP_USED,
            schema_fingerprint="fp-1",
            observed_at=datetime.now(timezone.utc),
            evidence=CatalogEvidence(kind="manual", reference="admission:1"),
        )


def test_principal_partition_is_never_the_raw_subject():
    signal = _signal(principal="alice@example.com")
    assert "alice" not in signal.principal_partition
    assert signal.principal_partition.startswith("sha256:")
    assert signal.principal_partition == hash_principal_partition("alice@example.com")


def test_build_usage_signal_is_deterministic_per_evidence_reference():
    first = _signal(evidence_ref="admission:same")
    second = _signal(evidence_ref="admission:same")
    assert first.signal_id == second.signal_id

    third = _signal(evidence_ref="admission:different")
    assert third.signal_id != first.signal_id


# --- record_usage_signals ----------------------------------------------------


def test_record_usage_signals_empty_is_a_no_op():
    store = CatalogStore.empty()
    update = record_usage_signals(store, signals=[])
    assert update.outcome == "no_op"
    assert update.store is store


def test_record_usage_signals_is_idempotent_on_signal_id():
    store = CatalogStore.empty()
    signal = _signal()

    first = record_usage_signals(store, signals=[signal])
    assert first.outcome == "recorded"
    assert first.recorded_signal_ids == (signal.signal_id,)

    second = record_usage_signals(first.store, signals=[signal])
    assert second.outcome == "no_op"
    assert second.duplicate_signal_ids == (signal.signal_id,)
    assert len(list(second.store.iter_usage_signals("demo"))) == 1


def test_record_usage_signals_batches_recorded_and_duplicate_in_one_call():
    store = CatalogStore.empty()
    existing = _signal(evidence_ref="admission:existing")
    store = record_usage_signals(store, signals=[existing]).store

    fresh = _signal(evidence_ref="admission:fresh")
    update = record_usage_signals(store, signals=[existing, fresh])
    assert update.outcome == "recorded"
    assert set(update.recorded_signal_ids) == {fresh.signal_id}
    assert set(update.duplicate_signal_ids) == {existing.signal_id}


def test_record_usage_signals_prunes_fifo_at_the_bounded_cap(monkeypatch):
    monkeypatch.setattr(usage_module, "_MAX_USAGE_SIGNALS", 2)
    store = CatalogStore.empty()
    signals = [_signal(evidence_ref=f"admission:{i}") for i in range(3)]
    update = record_usage_signals(store, signals=signals)
    stored_ids = {s["signal_id"] for s in update.store.to_dict()["usage_signals"]}
    assert len(stored_ids) == 2
    # the oldest (first-appended) signal was evicted
    assert signals[0].signal_id not in stored_ids
    assert signals[-1].signal_id in stored_ids


# --- should_emit_signal (anti-feedback-loop gate) ---------------------------


def test_should_emit_signal_true_when_no_catalog_entry_exists():
    store = CatalogStore.empty()
    assert should_emit_signal(store, connection_id="demo", target=_table_target())


def test_should_emit_signal_true_when_entry_is_verified():
    store = CatalogStore.from_dict(
        {
            "version": 2,
            "connections": {
                "demo": {
                    "tables": {
                        "orders": {
                            "description": "Verified.",
                            "provenance": {"status": "verified", "source_class": "verified"},
                        }
                    }
                }
            },
        }
    )
    assert should_emit_signal(store, connection_id="demo", target=_table_target())


@pytest.mark.parametrize("status", ["draft", "stale"])
def test_should_emit_signal_false_for_unreviewed_guesses(status):
    store = CatalogStore.from_dict(
        {
            "version": 2,
            "connections": {
                "demo": {
                    "tables": {
                        "orders": {
                            "description": "Guess.",
                            "provenance": {
                                "status": status,
                                "source_class": "inferred",
                                "confidence": 0.5,
                            },
                        }
                    }
                }
            },
        }
    )
    assert not should_emit_signal(store, connection_id="demo", target=_table_target())


def test_should_emit_signal_checks_relationship_provenance_specifically():
    store = CatalogStore.from_dict(
        {
            "version": 2,
            "connections": {
                "demo": {
                    "tables": {
                        "orders": {
                            "relationships": [
                                {
                                    "column": "customer_id",
                                    "to_table": "customers",
                                    "to_column": "id",
                                    "provenance": {
                                        "status": "draft",
                                        "source_class": "inferred",
                                        "confidence": 0.5,
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        }
    )
    assert not should_emit_signal(store, connection_id="demo", target=_relationship_target())


# --- InProcessUsageSignalBuffer ---------------------------------------------


def test_buffer_partitions_by_connection_independently():
    buffer = InProcessUsageSignalBuffer(max_size_per_connection=10)
    demo_signal = _signal(evidence_ref="a")
    other_signal = _signal(
        evidence_ref="b", connection_id="other", target=_relationship_target(connection_id="other")
    )
    buffer.enqueue(demo_signal)
    buffer.enqueue(other_signal)

    assert buffer.size("demo") == 1
    assert buffer.size("other") == 1
    drained_demo = buffer.drain("demo")
    assert drained_demo == [demo_signal]
    assert buffer.size("demo") == 0
    assert buffer.size("other") == 1  # unaffected by draining "demo"


def test_buffer_drops_oldest_on_overflow():
    buffer = InProcessUsageSignalBuffer(max_size_per_connection=2)
    signals = [_signal(evidence_ref=f"a{i}") for i in range(3)]
    for signal in signals:
        buffer.enqueue(signal)
    drained = buffer.drain("demo")
    assert drained == signals[1:]


def test_buffer_drain_of_empty_connection_returns_empty_list():
    buffer = InProcessUsageSignalBuffer(max_size_per_connection=10)
    assert buffer.drain("nonexistent") == []


# --- summarize_usage_signals -------------------------------------------------


def test_summarize_usage_signals_aggregates_support_by_target():
    store = CatalogStore.empty()
    signals = [_signal(principal=f"user-{i}", evidence_ref=f"admission:{i}") for i in range(3)]
    store = record_usage_signals(store, signals=signals).store

    summaries = summarize_usage_signals(store, "demo")
    assert len(summaries) == 1
    assert summaries[0].support == 3
    assert summaries[0].signal_count == 3
    assert summaries[0].target.table == "orders"


# --- CatalogUsageLearningMonitor ---------------------------------------------


def _catalog_with_snapshot(tmp_path, snapshot: ObservedSchemaSnapshot):
    from querygate.catalog.repository import CatalogFileUpdate

    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text("version: 2\nconnections:\n  demo:\n    tables: {}\n")
    repository = CatalogFileRepository(str(catalog_path))
    raw = CatalogStore.from_file(str(catalog_path)).to_dict()
    raw.setdefault("schema_snapshots", {})["demo"] = snapshot.model_dump(mode="json")
    repository.update(lambda store: CatalogFileUpdate(store.replace(raw), None))
    return catalog_path


@pytest.mark.asyncio
async def test_monitor_run_once_flushes_buffer_then_learns(tmp_path):
    snapshot = _snapshot()
    catalog_path = _catalog_with_snapshot(tmp_path, snapshot)

    monitor = CatalogUsageLearningMonitor(catalog_file=str(catalog_path), interval_seconds=60)
    # 5 distinct principals: enough to clear both MIN_DISTINCT_PRINCIPALS and
    # MIN_CONFIDENCE (support / FULL_CONFIDENCE_SUPPORT >= 0.6).
    for principal in ("user-1", "user-2", "user-3", "user-4", "user-5"):
        signal = build_usage_signal(
            connection_id="demo",
            principal_subject=principal,
            target=_relationship_target(),
            kind=CatalogUsageSignalKind.RELATIONSHIP_USED,
            schema_fingerprint=snapshot.fingerprint,
            evidence_reference=f"admission:{principal}",
        )
        usage_module.enqueue_usage_signal(signal)

    update = await monitor.run_once("demo")

    assert update.outcome == "generated"
    assert update.added_count == 1
    reloaded = CatalogStore.from_file(str(catalog_path))
    assert len(list(reloaded.iter_usage_signals("demo"))) == 5
    assert len(list(reloaded.iter_draft_proposals("demo"))) == 1


@pytest.mark.asyncio
async def test_monitor_persists_signals_even_if_learning_step_finds_no_evidence(tmp_path):
    """A learner exception/no-evidence outcome must never lose already-
    drained signals — they were persisted in a separate, earlier write.
    """

    snapshot = _snapshot()
    catalog_path = _catalog_with_snapshot(tmp_path, snapshot)

    monitor = CatalogUsageLearningMonitor(catalog_file=str(catalog_path), interval_seconds=60)
    # Only one distinct principal — below MIN_DISTINCT_PRINCIPALS, so the
    # learner produces no proposal, but the signal itself must still land.
    signal = build_usage_signal(
        connection_id="demo",
        principal_subject="solo-user",
        target=_relationship_target(),
        kind=CatalogUsageSignalKind.RELATIONSHIP_USED,
        schema_fingerprint=snapshot.fingerprint,
        evidence_reference="admission:solo",
    )
    usage_module.enqueue_usage_signal(signal)

    update = await monitor.run_once("demo")
    assert update.outcome == "no_evidence"
    reloaded = CatalogStore.from_file(str(catalog_path))
    assert len(list(reloaded.iter_usage_signals("demo"))) == 1
