"""Operator semantic-memory CLI workflow tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml

from querygate.catalog.benchmark import evaluate_benchmark, load_benchmark
from querygate.catalog.loader import CatalogStore
from querygate.catalog.providers import ManualDraftBatch, SemanticMemoryProviderMode
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.catalog_cli import (
    approve_proposal,
    default_benchmark_path,
    edit_proposal,
    generate_manual_drafts_file,
    list_proposals,
    list_versions,
    publish_proposal,
    reject_proposal,
    rollback_version,
    show_proposal,
    show_version,
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


def test_semantic_memory_configuration_is_safe_by_default_and_rejects_live_mode():
    config = AppConfig()
    assert config.semantic_memory_provider == SemanticMemoryProviderMode.DISABLED
    assert config.semantic_memory_refresh_enabled is False

    with pytest.raises(Exception):
        AppConfig(semantic_memory_provider="hosted")
    with pytest.raises(Exception, match="CATALOG_FILE"):
        AppConfig(semantic_memory_refresh_enabled=True, catalog_file=None)
