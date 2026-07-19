"""Operator semantic-memory CLI workflow tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml

from querygate.catalog.benchmark import evaluate_benchmark, load_benchmark
from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogDraftObjectType,
    CatalogDraftTarget,
    CatalogUsageSignalKind,
)
from querygate.catalog.providers import ManualDraftBatch, SemanticMemoryProviderMode
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.catalog.usage import build_usage_signal
from querygate.catalog_cli import (
    approve_proposal,
    default_benchmark_path,
    delete_proposal,
    delete_version,
    edit_proposal,
    export_connection,
    generate_manual_drafts_file,
    import_connection,
    learn,
    list_proposals,
    list_usage_signals,
    list_versions,
    publish_proposal,
    reject_proposal,
    rollback_version,
    show_proposal,
    show_version,
    submit_usage_signals,
)
from querygate.core.config import AppConfig
from querygate.core.exceptions import CatalogGovernanceError, NotFoundError


def _catalog_file(tmp_path: Path) -> tuple[Path, ObservedSchemaSnapshot]:
    metadata = sa.MetaData()
    table = sa.Table("customers", metadata, sa.Column("id", sa.Integer, primary_key=True))
    snapshot = ObservedSchemaSnapshot.from_tables("demo", [table])
    path = tmp_path / "catalog.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "connections": {"demo": {"tables": {"customers": {}}}},
                "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
            },
            sort_keys=False,
        )
    )
    return path, snapshot


def test_manual_cli_workflow_persists_drafts_and_is_idempotent(tmp_path):
    catalog_path, snapshot = _catalog_file(tmp_path)
    input_path = tmp_path / "drafts.yaml"
    input_path.write_text(
        yaml.safe_dump(
            {
                "generation_id": "cli-onboarding-1",
                "connection_id": "demo",
                "schema_fingerprint": snapshot.fingerprint,
                "suggestions": [
                    {
                        "target": {
                            "connection_id": "demo",
                            "object_type": "table",
                            "table": "customers",
                        },
                        "content": {"description": "Customer master data."},
                        "confidence": 0.8,
                    }
                ],
            },
            sort_keys=False,
        )
    )

    first = generate_manual_drafts_file(
        catalog_file=str(catalog_path),
        input_file=str(input_path),
        provider_mode=SemanticMemoryProviderMode.MANUAL,
    )
    second = generate_manual_drafts_file(
        catalog_file=str(catalog_path),
        input_file=str(input_path),
        provider_mode=SemanticMemoryProviderMode.MANUAL,
    )

    assert first.outcome == "generated"
    assert second.outcome == "idempotent"
    assert len(list(CatalogStore.from_file(str(catalog_path)).iter_draft_proposals())) == 1


def test_disabled_cli_mode_does_not_parse_or_mutate_provider_input(tmp_path):
    catalog_path, _snapshot = _catalog_file(tmp_path)
    before = catalog_path.read_bytes()

    update = generate_manual_drafts_file(
        catalog_file=str(catalog_path),
        input_file=str(tmp_path / "does-not-exist-and-must-not-be-opened.yaml"),
        provider_mode=SemanticMemoryProviderMode.DISABLED,
    )

    assert update.outcome == "disabled"
    assert catalog_path.read_bytes() == before


def test_packaged_benchmark_is_executable_offline():
    report = evaluate_benchmark(load_benchmark(default_benchmark_path()))
    assert report.passed


def test_documented_manual_draft_example_is_structurally_valid():
    example_path = Path(__file__).parents[2] / "examples" / "catalog_drafts.example.yaml"
    batch = ManualDraftBatch.model_validate(yaml.safe_load(example_path.read_text()))
    assert batch.provider_id == "manual-only"


def _catalog_with_pending_proposal(tmp_path: Path) -> tuple[Path, str]:
    catalog_path, snapshot = _catalog_file(tmp_path)
    input_path = tmp_path / "drafts.yaml"
    input_path.write_text(
        yaml.safe_dump(
            {
                "generation_id": "cli-review-1",
                "connection_id": "demo",
                "schema_fingerprint": snapshot.fingerprint,
                "suggestions": [
                    {
                        "target": {
                            "connection_id": "demo",
                            "object_type": "table",
                            "table": "customers",
                        },
                        "content": {"description": "Customer master data."},
                        "confidence": 0.8,
                    }
                ],
            },
            sort_keys=False,
        )
    )
    generate_manual_drafts_file(
        catalog_file=str(catalog_path),
        input_file=str(input_path),
        provider_mode=SemanticMemoryProviderMode.MANUAL,
    )
    proposal_id = list(CatalogStore.from_file(str(catalog_path)).iter_draft_proposals())[
        0
    ].proposal_id
    return catalog_path, proposal_id


def test_cli_full_review_publish_rollback_lifecycle(tmp_path):
    catalog_path, proposal_id = _catalog_with_pending_proposal(tmp_path)

    proposals = list_proposals(catalog_file=str(catalog_path), connection_id="demo")
    assert len(proposals) == 1
    assert proposals[0]["review_status"] == "pending"

    shown = show_proposal(
        catalog_file=str(catalog_path), connection_id="demo", proposal_id=proposal_id
    )
    assert shown["proposal_id"] == proposal_id

    content_file = tmp_path / "edit.yaml"
    content_file.write_text(yaml.safe_dump({"description": "Edited by a CLI reviewer."}))
    edit_result = edit_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="cli-reviewer",
        content_file=str(content_file),
    )
    assert edit_result["outcome"] == "edited"

    approve_result = approve_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="cli-reviewer",
    )
    assert approve_result["outcome"] == "approved"

    publish_result = publish_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="cli-publisher",
    )
    assert publish_result["outcome"] == "published"
    version_id = publish_result["version_id"]

    reloaded = CatalogStore.from_file(str(catalog_path))
    assert reloaded.get_table("demo", "customers").description == "Edited by a CLI reviewer."

    versions = list_versions(catalog_file=str(catalog_path), connection_id="demo")
    assert len(versions) == 1
    assert versions[0]["version_id"] == version_id

    version_detail = show_version(
        catalog_file=str(catalog_path), connection_id="demo", version_id=version_id
    )
    assert version_detail["action"] == "publish"

    rollback_result = rollback_version(
        catalog_file=str(catalog_path),
        connection_id="demo",
        version_id=version_id,
        actor="cli-operator",
    )
    assert rollback_result["outcome"] == "rolled_back"
    assert (
        CatalogStore.from_file(str(catalog_path)).get_table("demo", "customers").description is None
    )


def test_cli_reject_requires_reason_and_records_it(tmp_path):
    catalog_path, proposal_id = _catalog_with_pending_proposal(tmp_path)
    result = reject_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="cli-reviewer",
        reason="not accurate enough",
    )
    assert result["outcome"] == "rejected"
    proposal = CatalogStore.from_file(str(catalog_path)).get_draft_proposal(proposal_id)
    assert proposal.review_status.value == "rejected"
    assert proposal.review_history[-1].reason == "not accurate enough"

    with pytest.raises(CatalogGovernanceError):
        reject_proposal(
            catalog_file=str(catalog_path),
            connection_id="demo",
            proposal_id=proposal_id,
            actor="cli-reviewer",
            reason="again",
        )


def test_cli_show_unknown_proposal_raises_not_found(tmp_path):
    catalog_path, _snapshot = _catalog_file(tmp_path)
    with pytest.raises(NotFoundError):
        show_proposal(catalog_file=str(catalog_path), connection_id="demo", proposal_id="nope")


def _cli_publish(tmp_path: Path) -> tuple[Path, str]:
    catalog_path, proposal_id = _catalog_with_pending_proposal(tmp_path)
    approve_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="reviewer",
    )
    publish_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="publisher",
    )
    return catalog_path, proposal_id


def test_cli_export_import_round_trip(tmp_path):
    catalog_path, proposal_id = _cli_publish(tmp_path)

    import json as _json

    bundle = export_connection(catalog_file=str(catalog_path), connection_id="demo")
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(_json.dumps(bundle))

    fresh_catalog = tmp_path / "restored.yaml"
    fresh_catalog.write_text(yaml.safe_dump({"version": 2, "connections": {}}))
    result = import_connection(
        catalog_file=str(fresh_catalog),
        connection_id="demo",
        bundle_file=str(bundle_file),
        actor="operator",
    )
    assert result["outcome"] == "imported"

    restored = CatalogStore.from_file(str(fresh_catalog))
    assert restored.get_table("demo", "customers").description == "Customer master data."
    assert restored.get_draft_proposal(proposal_id).review_status.value == "published"


def test_cli_delete_rejected_proposal(tmp_path):
    catalog_path, proposal_id = _catalog_with_pending_proposal(tmp_path)
    from querygate.catalog_cli import reject_proposal as _reject

    _reject(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="reviewer",
        reason="no longer needed",
    )
    result = delete_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="operator",
    )
    assert result["outcome"] == "deleted"
    assert CatalogStore.from_file(str(catalog_path)).get_draft_proposal(proposal_id) is None


def test_cli_delete_published_proposal_requires_rollback_first(tmp_path):
    catalog_path, proposal_id = _cli_publish(tmp_path)
    with pytest.raises(CatalogGovernanceError, match="still live"):
        delete_proposal(
            catalog_file=str(catalog_path),
            connection_id="demo",
            proposal_id=proposal_id,
            actor="operator",
        )

    rollback_version(
        catalog_file=str(catalog_path), connection_id="demo", version_id="1", actor="operator"
    )
    result = delete_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="operator",
    )
    assert result["outcome"] == "deleted"


def test_cli_delete_version_only_allows_rollback_records(tmp_path):
    catalog_path, _proposal_id = _cli_publish(tmp_path)
    with pytest.raises(CatalogGovernanceError, match="rollback record"):
        delete_version(
            catalog_file=str(catalog_path), connection_id="demo", version_id="1", actor="operator"
        )

    rollback_version(
        catalog_file=str(catalog_path), connection_id="demo", version_id="1", actor="operator"
    )
    result = delete_version(
        catalog_file=str(catalog_path), connection_id="demo", version_id="2", actor="operator"
    )
    assert result["outcome"] == "deleted"


def test_semantic_memory_configuration_is_safe_by_default_and_rejects_live_mode():
    config = AppConfig()
    assert config.semantic_memory_provider == SemanticMemoryProviderMode.DISABLED
    assert config.semantic_memory_refresh_enabled is False
    assert config.semantic_memory_usage_signals_enabled is False
    assert config.semantic_memory_learning_enabled is False

    with pytest.raises(Exception):
        AppConfig(semantic_memory_provider="hosted")
    with pytest.raises(Exception, match="CATALOG_FILE"):
        AppConfig(semantic_memory_refresh_enabled=True, catalog_file=None)
    with pytest.raises(Exception, match="CATALOG_FILE"):
        AppConfig(semantic_memory_usage_signals_enabled=True, catalog_file=None)
    with pytest.raises(Exception, match="CATALOG_FILE"):
        AppConfig(semantic_memory_learning_enabled=True, catalog_file=None)


# --- TODO item 32C: usage-signal / usage-learning CLI subcommands -----------


def _catalog_with_orders(tmp_path: Path) -> tuple[Path, ObservedSchemaSnapshot]:
    metadata = sa.MetaData()
    customers = sa.Table("customers", metadata, sa.Column("id", sa.Integer, primary_key=True))
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id")),
    )
    snapshot = ObservedSchemaSnapshot.from_tables("demo", [customers, orders])
    path = tmp_path / "catalog.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "connections": {"demo": {"tables": {}}},
                "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
            },
            sort_keys=False,
        )
    )
    return path, snapshot


def _relationship_target() -> CatalogDraftTarget:
    return CatalogDraftTarget(
        connection_id="demo",
        object_type=CatalogDraftObjectType.RELATIONSHIP,
        table="orders",
        column="customer_id",
        to_table="customers",
        to_column="id",
    )


def test_submit_usage_signals_is_idempotent_and_records_via_the_file_lock(tmp_path):
    catalog_path, snapshot = _catalog_with_orders(tmp_path)
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
        for i in range(5)
    ]
    input_path = tmp_path / "signals.yaml"
    input_path.write_text(
        yaml.safe_dump(
            [signal.model_dump(mode="json", exclude_none=True) for signal in signals],
            sort_keys=False,
        )
    )

    first = submit_usage_signals(catalog_file=str(catalog_path), input_file=str(input_path))
    assert first["outcome"] == "recorded"
    assert first["recorded_count"] == 5
    assert first["duplicate_count"] == 0

    second = submit_usage_signals(catalog_file=str(catalog_path), input_file=str(input_path))
    assert second["outcome"] == "no_op"
    assert second["duplicate_count"] == 5

    assert len(list(CatalogStore.from_file(str(catalog_path)).iter_usage_signals("demo"))) == 5


def test_list_usage_signals_reports_aggregated_support(tmp_path):
    catalog_path, snapshot = _catalog_with_orders(tmp_path)
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
        for i in range(5)
    ]
    input_path = tmp_path / "signals.yaml"
    input_path.write_text(
        yaml.safe_dump(
            [signal.model_dump(mode="json", exclude_none=True) for signal in signals],
            sort_keys=False,
        )
    )
    submit_usage_signals(catalog_file=str(catalog_path), input_file=str(input_path))

    summaries = list_usage_signals(catalog_file=str(catalog_path), connection_id="demo")
    assert len(summaries) == 1
    assert summaries[0]["kind"] == "relationship_used"
    assert summaries[0]["support"] == 5
    assert summaries[0]["table"] == "orders"
    assert summaries[0]["to_table"] == "customers"


def test_learn_generates_a_proposal_that_flows_through_the_normal_review_lifecycle(tmp_path):
    catalog_path, snapshot = _catalog_with_orders(tmp_path)
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
        for i in range(5)
    ]
    input_path = tmp_path / "signals.yaml"
    input_path.write_text(
        yaml.safe_dump(
            [signal.model_dump(mode="json", exclude_none=True) for signal in signals],
            sort_keys=False,
        )
    )
    submit_usage_signals(catalog_file=str(catalog_path), input_file=str(input_path))

    result = learn(catalog_file=str(catalog_path), connection_id="demo")
    assert result["outcome"] == "generated"
    assert result["added_proposal_count"] == 1

    proposal_id = list(CatalogStore.from_file(str(catalog_path)).iter_draft_proposals("demo"))[
        0
    ].proposal_id
    proposal = show_proposal(
        catalog_file=str(catalog_path), connection_id="demo", proposal_id=proposal_id
    )
    assert proposal["review_status"] == "pending"

    approve_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="reviewer",
    )
    publish_result = publish_proposal(
        catalog_file=str(catalog_path),
        connection_id="demo",
        proposal_id=proposal_id,
        actor="publisher",
    )
    assert publish_result["outcome"] == "published"

    reloaded = CatalogStore.from_file(str(catalog_path))
    published_table = reloaded.get_table("demo", "orders")
    relationship = next(r for r in published_table.relationships if r.column == "customer_id")
    assert relationship.provenance.source_class.value == "verified"


def test_learn_without_a_schema_snapshot_raises_value_error(tmp_path):
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(yaml.safe_dump({"version": 2, "connections": {}}))
    with pytest.raises(ValueError, match="schema snapshot"):
        learn(catalog_file=str(catalog_path), connection_id="demo")
