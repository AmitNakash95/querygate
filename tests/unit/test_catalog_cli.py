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
from querygate.catalog_cli import default_benchmark_path, generate_manual_drafts_file
from querygate.core.config import AppConfig


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


def test_semantic_memory_configuration_is_safe_by_default_and_rejects_live_mode():
    config = AppConfig()
    assert config.semantic_memory_provider == SemanticMemoryProviderMode.DISABLED
    assert config.semantic_memory_refresh_enabled is False

    with pytest.raises(Exception):
        AppConfig(semantic_memory_provider="hosted")
    with pytest.raises(Exception, match="CATALOG_FILE"):
        AppConfig(semantic_memory_refresh_enabled=True, catalog_file=None)
