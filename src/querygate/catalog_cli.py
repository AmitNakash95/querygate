"""Operator CLI for 32A-2 refresh, manual drafts, and deterministic evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib import resources
from pathlib import Path
from typing import Optional

import yaml

from querygate.catalog.benchmark import evaluate_benchmark, load_benchmark
from querygate.catalog.generation import CatalogGenerationUpdate, generate_catalog_drafts
from querygate.catalog.loader import CatalogStore
from querygate.catalog.providers import (
    ManualDraftBatch,
    ManualSemanticMemoryProvider,
    SemanticGenerationRequest,
    SemanticMemoryProviderMode,
)
from querygate.catalog.refresh import (
    CatalogRefreshUpdate,
    refresh_catalog_schema,
    scan_connection_schema,
)
from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate


def default_benchmark_path() -> str:
    return str(
        resources.files("querygate.catalog")
        .joinpath("benchmark_data")
        .joinpath("semantic_memory_v1.yaml")
    )


def generate_manual_drafts_file(
    *,
    catalog_file: str,
    input_file: str,
    provider_mode: SemanticMemoryProviderMode,
    purpose: str = "onboarding",
    max_proposals: int = 100,
) -> CatalogGenerationUpdate:
    repository = CatalogFileRepository(catalog_file)
    if provider_mode == SemanticMemoryProviderMode.DISABLED:
        # The kill switch does not even parse provider input.
        return CatalogGenerationUpdate(
            store=CatalogStore.from_file(catalog_file),
            outcome="disabled",
            generation_id="disabled",
        )

    raw = yaml.safe_load(Path(input_file).read_text()) or {}
    batch = ManualDraftBatch.model_validate(raw)

    def _apply(store):
        snapshot = store.get_schema_snapshot(batch.connection_id)
        if snapshot is None:
            raise ValueError("manual draft generation requires a persisted schema snapshot")
        request = SemanticGenerationRequest(
            generation_id=batch.generation_id,
            connection_id=batch.connection_id,
            snapshot=snapshot,
            purpose=purpose,
            max_proposals=max_proposals,
        )
        update = generate_catalog_drafts(
            store,
            request=request,
            provider=ManualSemanticMemoryProvider(batch),
        )
        return CatalogFileUpdate(update.store, update)

    return repository.update(_apply)


async def refresh_catalog_file(
    *, catalog_file: str, connection_id: str, max_tables: int = 500
) -> CatalogRefreshUpdate:
    repository = CatalogFileRepository(catalog_file)

    async def _apply(store):
        snapshot = await scan_connection_schema(connection_id, max_tables=max_tables)
        update = refresh_catalog_schema(store, snapshot)
        return CatalogFileUpdate(update.store, update)

    return await repository.update_async(_apply)


def _catalog_path(argument: Optional[str], configured: Optional[str]) -> str:
    path = argument or configured
    if not path:
        raise ValueError("--catalog-file or CATALOG_FILE is required")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="querygate-semantic-memory",
        description="Refresh row-free schema memory, import manual drafts, or run its benchmark.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("--catalog-file")
    refresh_parser.add_argument("--connection", required=True)
    refresh_parser.add_argument("--max-tables", type=int)

    generate_parser = subparsers.add_parser("generate-drafts")
    generate_parser.add_argument("--catalog-file")
    generate_parser.add_argument("--input-file", required=True)
    generate_parser.add_argument(
        "--purpose", choices=["onboarding", "refresh", "correction"], default="onboarding"
    )
    generate_parser.add_argument("--max-proposals", type=int, default=100)

    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--benchmark", default=default_benchmark_path())

    args = parser.parse_args()
    from querygate.core.config import config

    try:
        if args.command == "evaluate":
            report = evaluate_benchmark(load_benchmark(args.benchmark))
            print(report.model_dump_json(indent=2))
            if not report.passed:
                sys.exit(1)
            return

        catalog_file = _catalog_path(args.catalog_file, config.catalog_file)
        if args.command == "refresh":
            update = asyncio.run(
                refresh_catalog_file(
                    catalog_file=catalog_file,
                    connection_id=args.connection,
                    max_tables=args.max_tables or config.semantic_memory_refresh_max_tables,
                )
            )
            print(
                json.dumps(
                    {
                        "connection_id": update.connection_id,
                        "changed": update.changed,
                        "before_fingerprint": update.before_fingerprint,
                        "after_fingerprint": update.after_fingerprint,
                        "stale_entry_count": len(update.stale_entry_ids),
                    },
                    indent=2,
                )
            )
            return

        update = generate_manual_drafts_file(
            catalog_file=catalog_file,
            input_file=args.input_file,
            provider_mode=config.semantic_memory_provider,
            purpose=args.purpose,
            max_proposals=args.max_proposals,
        )
        print(
            json.dumps(
                {
                    "generation_id": update.generation_id,
                    "outcome": update.outcome,
                    "added_proposal_count": update.added_count,
                },
                indent=2,
            )
        )
    except Exception as exc:
        # Avoid printing raw driver/provider exceptions, which can contain
        # credentials or untrusted server text. Operators get a stable type.
        print(f"semantic-memory command failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
