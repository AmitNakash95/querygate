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

from querygate.catalog import governance
from querygate.catalog.benchmark import evaluate_benchmark, load_benchmark
from querygate.catalog.generation import CatalogGenerationUpdate, generate_catalog_drafts
from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogDraftContent,
    CatalogDraftProposal,
    CatalogExportBundle,
    CatalogVersionRecord,
)
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
from querygate.core.exceptions import NotFoundError


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


def _proposal_json(proposal: CatalogDraftProposal) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "generation_id": proposal.generation_id,
        "target": proposal.target.model_dump(mode="json", exclude_none=True),
        "content": proposal.content.model_dump(mode="json", exclude_none=True),
        "review_status": proposal.review_status.value,
        "schema_status": proposal.provenance.status.value,
        "published_entry_id": proposal.published_entry_id,
        "published_version_id": proposal.published_version_id,
        "review_history": [
            event.model_dump(mode="json", exclude_none=True) for event in proposal.review_history
        ],
    }


def _version_json(record: CatalogVersionRecord) -> dict:
    return record.model_dump(mode="json", exclude_none=True)


def list_proposals(
    *, catalog_file: str, connection_id: str, review_status: Optional[str] = None
) -> list[dict]:
    store = CatalogStore.from_file(catalog_file)
    proposals = list(store.iter_draft_proposals(connection_id))
    if review_status is not None:
        proposals = [p for p in proposals if p.review_status.value == review_status]
    return [_proposal_json(p) for p in proposals]


def show_proposal(*, catalog_file: str, connection_id: str, proposal_id: str) -> dict:
    store = CatalogStore.from_file(catalog_file)
    proposal = store.get_draft_proposal(proposal_id)
    if proposal is None or proposal.target.connection_id != connection_id:
        raise NotFoundError(f"Unknown catalog proposal: {proposal_id!r}")
    return _proposal_json(proposal)


def preview_publish(
    *,
    catalog_file: str,
    policy_file: str,
    connection_id: str,
    proposal_id: str,
    principal_subject: str,
) -> dict:
    from querygate.core.auth import Principal
    from querygate.policy.loader import PolicyStore

    store = CatalogStore.from_file(catalog_file)
    policy_store = PolicyStore.from_file(policy_file)
    policy = policy_store.get(connection_id, principal=Principal(subject=principal_subject))
    preview = governance.preview_publish(
        store, proposal_id=proposal_id, connection_id=connection_id, policy=policy
    )
    return preview.model_dump(mode="json", exclude_none=True)


def _governed_update(catalog_file: str, mutator) -> governance.CatalogGovernanceUpdate:
    repository = CatalogFileRepository(catalog_file)

    def _apply(store: CatalogStore) -> CatalogFileUpdate[governance.CatalogGovernanceUpdate]:
        update = mutator(store)
        return CatalogFileUpdate(update.store, update)

    return repository.update(_apply)


def edit_proposal(
    *, catalog_file: str, connection_id: str, proposal_id: str, actor: str, content_file: str
) -> dict:
    raw = yaml.safe_load(Path(content_file).read_text()) or {}
    content = CatalogDraftContent.model_validate(raw)
    update = _governed_update(
        catalog_file,
        lambda store: governance.edit_proposal(
            store,
            proposal_id=proposal_id,
            connection_id=connection_id,
            actor=actor,
            content=content,
        ),
    )
    return {"outcome": update.outcome, "proposal_id": update.proposal_id}


def approve_proposal(
    *, catalog_file: str, connection_id: str, proposal_id: str, actor: str
) -> dict:
    update = _governed_update(
        catalog_file,
        lambda store: governance.approve_proposal(
            store, proposal_id=proposal_id, connection_id=connection_id, actor=actor
        ),
    )
    return {"outcome": update.outcome, "proposal_id": update.proposal_id}


def reject_proposal(
    *, catalog_file: str, connection_id: str, proposal_id: str, actor: str, reason: str
) -> dict:
    update = _governed_update(
        catalog_file,
        lambda store: governance.reject_proposal(
            store,
            proposal_id=proposal_id,
            connection_id=connection_id,
            actor=actor,
            reason=reason,
        ),
    )
    return {"outcome": update.outcome, "proposal_id": update.proposal_id}


def publish_proposal(
    *, catalog_file: str, connection_id: str, proposal_id: str, actor: str
) -> dict:
    update = _governed_update(
        catalog_file,
        lambda store: governance.publish_proposal(
            store, proposal_id=proposal_id, connection_id=connection_id, actor=actor
        ),
    )
    return {
        "outcome": update.outcome,
        "proposal_id": update.proposal_id,
        "version_id": update.version_id,
        "entry_id": update.entry_id,
    }


def list_versions(*, catalog_file: str, connection_id: str) -> list[dict]:
    store = CatalogStore.from_file(catalog_file)
    return [_version_json(record) for record in store.iter_version_history(connection_id)]


def show_version(*, catalog_file: str, connection_id: str, version_id: str) -> dict:
    store = CatalogStore.from_file(catalog_file)
    record = store.get_version_record(version_id)
    if record is None or record.changes[0].target.connection_id != connection_id:
        raise NotFoundError(f"Unknown catalog version: {version_id!r}")
    return _version_json(record)


def rollback_version(*, catalog_file: str, connection_id: str, version_id: str, actor: str) -> dict:
    update = _governed_update(
        catalog_file,
        lambda store: governance.rollback_version(
            store, version_id=version_id, connection_id=connection_id, actor=actor
        ),
    )
    return {
        "outcome": update.outcome,
        "version_id": update.version_id,
        "rolled_back_version_id": update.rolled_back_version_id,
    }


def export_connection(*, catalog_file: str, connection_id: str) -> dict:
    store = CatalogStore.from_file(catalog_file)
    bundle = governance.export_connection(store, connection_id=connection_id)
    # round_trip=True omits computed fields (e.g. provenance precedence) so
    # the exported JSON can be fed straight back into `import_connection`,
    # the same reason `CatalogStore.to_dict()` uses it.
    return bundle.model_dump(mode="json", exclude_none=True, round_trip=True)


def import_connection(
    *, catalog_file: str, connection_id: str, bundle_file: str, actor: str
) -> dict:
    raw = yaml.safe_load(Path(bundle_file).read_text()) or {}
    bundle = CatalogExportBundle.model_validate(raw)
    update = _governed_update(
        catalog_file,
        lambda store: governance.import_connection(
            store, bundle=bundle, connection_id=connection_id, actor=actor
        ),
    )
    return {"outcome": update.outcome}


def delete_proposal(*, catalog_file: str, connection_id: str, proposal_id: str, actor: str) -> dict:
    update = _governed_update(
        catalog_file,
        lambda store: governance.delete_proposal(
            store, proposal_id=proposal_id, connection_id=connection_id, actor=actor
        ),
    )
    return {"outcome": update.outcome, "proposal_id": update.proposal_id}


def delete_version(*, catalog_file: str, connection_id: str, version_id: str, actor: str) -> dict:
    update = _governed_update(
        catalog_file,
        lambda store: governance.delete_version_record(
            store, version_id=version_id, connection_id=connection_id, actor=actor
        ),
    )
    return {"outcome": update.outcome, "version_id": update.version_id}


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

    list_proposals_parser = subparsers.add_parser("list-proposals")
    list_proposals_parser.add_argument("--catalog-file")
    list_proposals_parser.add_argument("--connection", required=True)
    list_proposals_parser.add_argument(
        "--status", choices=["pending", "approved", "rejected", "published"], default=None
    )

    show_proposal_parser = subparsers.add_parser("show-proposal")
    show_proposal_parser.add_argument("--catalog-file")
    show_proposal_parser.add_argument("--connection", required=True)
    show_proposal_parser.add_argument("--proposal-id", required=True)

    edit_proposal_parser = subparsers.add_parser("edit-proposal")
    edit_proposal_parser.add_argument("--catalog-file")
    edit_proposal_parser.add_argument("--connection", required=True)
    edit_proposal_parser.add_argument("--proposal-id", required=True)
    edit_proposal_parser.add_argument("--actor", required=True)
    edit_proposal_parser.add_argument("--content-file", required=True)

    approve_proposal_parser = subparsers.add_parser("approve-proposal")
    approve_proposal_parser.add_argument("--catalog-file")
    approve_proposal_parser.add_argument("--connection", required=True)
    approve_proposal_parser.add_argument("--proposal-id", required=True)
    approve_proposal_parser.add_argument("--actor", required=True)

    reject_proposal_parser = subparsers.add_parser("reject-proposal")
    reject_proposal_parser.add_argument("--catalog-file")
    reject_proposal_parser.add_argument("--connection", required=True)
    reject_proposal_parser.add_argument("--proposal-id", required=True)
    reject_proposal_parser.add_argument("--actor", required=True)
    reject_proposal_parser.add_argument("--reason", required=True)

    publish_proposal_parser = subparsers.add_parser("publish-proposal")
    publish_proposal_parser.add_argument("--catalog-file")
    publish_proposal_parser.add_argument("--connection", required=True)
    publish_proposal_parser.add_argument("--proposal-id", required=True)
    publish_proposal_parser.add_argument("--actor", required=True)

    preview_publish_parser = subparsers.add_parser("preview-publish")
    preview_publish_parser.add_argument("--catalog-file")
    preview_publish_parser.add_argument("--policy-file")
    preview_publish_parser.add_argument("--connection", required=True)
    preview_publish_parser.add_argument("--proposal-id", required=True)
    preview_publish_parser.add_argument("--principal-subject", required=True)

    list_versions_parser = subparsers.add_parser("list-versions")
    list_versions_parser.add_argument("--catalog-file")
    list_versions_parser.add_argument("--connection", required=True)

    show_version_parser = subparsers.add_parser("show-version")
    show_version_parser.add_argument("--catalog-file")
    show_version_parser.add_argument("--connection", required=True)
    show_version_parser.add_argument("--version-id", required=True)

    rollback_version_parser = subparsers.add_parser("rollback-version")
    rollback_version_parser.add_argument("--catalog-file")
    rollback_version_parser.add_argument("--connection", required=True)
    rollback_version_parser.add_argument("--version-id", required=True)
    rollback_version_parser.add_argument("--actor", required=True)

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--catalog-file")
    export_parser.add_argument("--connection", required=True)
    export_parser.add_argument("--output-file", help="Write the bundle here instead of stdout")

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--catalog-file")
    import_parser.add_argument("--connection", required=True)
    import_parser.add_argument("--bundle-file", required=True)
    import_parser.add_argument("--actor", required=True)

    delete_proposal_parser = subparsers.add_parser("delete-proposal")
    delete_proposal_parser.add_argument("--catalog-file")
    delete_proposal_parser.add_argument("--connection", required=True)
    delete_proposal_parser.add_argument("--proposal-id", required=True)
    delete_proposal_parser.add_argument("--actor", required=True)

    delete_version_parser = subparsers.add_parser("delete-version")
    delete_version_parser.add_argument("--catalog-file")
    delete_version_parser.add_argument("--connection", required=True)
    delete_version_parser.add_argument("--version-id", required=True)
    delete_version_parser.add_argument("--actor", required=True)

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

        if args.command == "generate-drafts":
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
            return

        if args.command == "list-proposals":
            print(
                json.dumps(
                    list_proposals(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        review_status=args.status,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "show-proposal":
            print(
                json.dumps(
                    show_proposal(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        proposal_id=args.proposal_id,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "edit-proposal":
            print(
                json.dumps(
                    edit_proposal(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        proposal_id=args.proposal_id,
                        actor=args.actor,
                        content_file=args.content_file,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "approve-proposal":
            print(
                json.dumps(
                    approve_proposal(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        proposal_id=args.proposal_id,
                        actor=args.actor,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "reject-proposal":
            print(
                json.dumps(
                    reject_proposal(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        proposal_id=args.proposal_id,
                        actor=args.actor,
                        reason=args.reason,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "publish-proposal":
            print(
                json.dumps(
                    publish_proposal(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        proposal_id=args.proposal_id,
                        actor=args.actor,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "preview-publish":
            print(
                json.dumps(
                    preview_publish(
                        catalog_file=catalog_file,
                        policy_file=_catalog_path(args.policy_file, config.policy_file),
                        connection_id=args.connection,
                        proposal_id=args.proposal_id,
                        principal_subject=args.principal_subject,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "list-versions":
            print(
                json.dumps(
                    list_versions(catalog_file=catalog_file, connection_id=args.connection),
                    indent=2,
                )
            )
            return
        if args.command == "show-version":
            print(
                json.dumps(
                    show_version(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        version_id=args.version_id,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "rollback-version":
            print(
                json.dumps(
                    rollback_version(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        version_id=args.version_id,
                        actor=args.actor,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "export":
            payload = json.dumps(
                export_connection(catalog_file=catalog_file, connection_id=args.connection),
                indent=2,
            )
            if args.output_file:
                Path(args.output_file).write_text(payload)
                print(
                    json.dumps({"outcome": "exported", "output_file": args.output_file}, indent=2)
                )
            else:
                print(payload)
            return
        if args.command == "import":
            print(
                json.dumps(
                    import_connection(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        bundle_file=args.bundle_file,
                        actor=args.actor,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "delete-proposal":
            print(
                json.dumps(
                    delete_proposal(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        proposal_id=args.proposal_id,
                        actor=args.actor,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "delete-version":
            print(
                json.dumps(
                    delete_version(
                        catalog_file=catalog_file,
                        connection_id=args.connection,
                        version_id=args.version_id,
                        actor=args.actor,
                    ),
                    indent=2,
                )
            )
            return

        raise ValueError(f"Unknown command: {args.command!r}")
    except (ValueError, governance.CatalogGovernanceError, NotFoundError) as exc:
        # These messages are already client-actionable (bad status transition,
        # stale schema, a verified-content conflict) and never carry
        # credentials or driver text, unlike an arbitrary caught exception.
        print(f"semantic-memory command failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Avoid printing raw driver/provider exceptions, which can contain
        # credentials or untrusted server text. Operators get a stable type.
        print(f"semantic-memory command failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
