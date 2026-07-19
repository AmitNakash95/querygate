"""Quarantine manual provider output as durable inferred draft proposals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogDraftObjectType,
    CatalogDraftProposal,
    CatalogEntryProvenance,
    CatalogGenerationRecord,
)
from querygate.catalog.providers import (
    ManualDraftSuggestion,
    SemanticGenerationRequest,
    SemanticMemoryProvider,
)
from querygate.catalog.schema_memory import ObservedSchemaSnapshot, ObservedTable


@dataclass(frozen=True)
class CatalogGenerationUpdate:
    store: CatalogStore
    outcome: str
    generation_id: str
    added_proposal_ids: tuple[str, ...] = ()

    @property
    def added_count(self) -> int:
        return len(self.added_proposal_ids)


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _proposal_id(generation_id: str, suggestion: ManualDraftSuggestion) -> str:
    digest = _fingerprint(
        {
            "generation_id": generation_id,
            "suggestion": suggestion.model_dump(mode="json"),
        }
    )[7:39]
    return f"urn:querygate:catalog:draft:{digest}"


def _table(snapshot: ObservedSchemaSnapshot, name: str) -> Optional[ObservedTable]:
    target = name.casefold()
    return next((table for table in snapshot.tables if table.name.casefold() == target), None)


def _column_exists(table: Optional[ObservedTable], name: Optional[str]) -> bool:
    if table is None or name is None:
        return False
    target = name.casefold()
    return any(column.name.casefold() == target for column in table.columns)


def _validate_target(snapshot: ObservedSchemaSnapshot, suggestion: ManualDraftSuggestion) -> None:
    target = suggestion.target
    source_table = _table(snapshot, target.table)
    if source_table is None:
        raise ValueError(f"draft target table {target.table!r} is absent from the schema snapshot")
    if target.object_type == CatalogDraftObjectType.TABLE:
        return
    if not _column_exists(source_table, target.column):
        raise ValueError(
            f"draft target column {target.table!r}.{target.column!r} is absent from the snapshot"
        )
    if target.object_type == CatalogDraftObjectType.RELATIONSHIP:
        target_table = _table(snapshot, target.to_table or "")
        if not _column_exists(target_table, target.to_column):
            raise ValueError("relationship draft target is absent from the schema snapshot")


def generate_catalog_drafts(
    store: CatalogStore,
    *,
    request: SemanticGenerationRequest,
    provider: SemanticMemoryProvider,
) -> CatalogGenerationUpdate:
    """Generate an idempotent, non-published proposal batch."""

    current_snapshot = store.get_schema_snapshot(request.connection_id)
    if current_snapshot is None:
        raise ValueError("draft generation requires a persisted schema snapshot")
    if current_snapshot.fingerprint != request.snapshot.fingerprint:
        raise ValueError("draft generation request does not use the current schema snapshot")

    provider_result = provider.generate(request)
    if provider_result.outcome == "disabled":
        return CatalogGenerationUpdate(
            store=store,
            outcome="disabled",
            generation_id=request.generation_id,
        )

    if provider_result.provider_id is None or provider_result.prompt_template_version is None:
        raise ValueError("generated provider output requires provider and prompt identifiers")
    for suggestion in provider_result.suggestions:
        _validate_target(request.snapshot, suggestion)

    input_fingerprint = _fingerprint(
        {
            "request": request.model_dump(mode="json"),
            "provider_mode": provider_result.provider_mode,
            "provider_id": provider_result.provider_id,
            "prompt_template_version": provider_result.prompt_template_version,
            "created_by": provider_result.created_by,
            "created_at": (
                provider_result.created_at.isoformat()
                if provider_result.created_at is not None
                else None
            ),
            "suggestions": [
                suggestion.model_dump(mode="json") for suggestion in provider_result.suggestions
            ],
        }
    )
    existing_record = store.get_generation_record(request.generation_id)
    if existing_record is not None:
        if existing_record.input_fingerprint != input_fingerprint:
            raise ValueError("generation idempotency key was reused with different input")
        return CatalogGenerationUpdate(
            store=store,
            outcome="idempotent",
            generation_id=request.generation_id,
        )

    proposals: list[CatalogDraftProposal] = []
    for suggestion in provider_result.suggestions:
        proposal_id = _proposal_id(request.generation_id, suggestion)
        proposals.append(
            CatalogDraftProposal(
                proposal_id=proposal_id,
                generation_id=request.generation_id,
                target=suggestion.target,
                content=suggestion.content,
                provenance=CatalogEntryProvenance(
                    entry_id=proposal_id,
                    catalog_version=store.version,
                    source_class="inferred",
                    source_evidence=[
                        {"kind": "import", "reference": f"generation:{request.generation_id}"}
                    ],
                    confidence=suggestion.confidence,
                    status="draft",
                    schema_fingerprint=request.snapshot.fingerprint,
                    created_by=provider_result.created_by,
                    created_at=provider_result.created_at,
                    model_id=provider_result.provider_id,
                    prompt_template_version=provider_result.prompt_template_version,
                ),
            )
        )
    proposal_ids = [proposal.proposal_id for proposal in proposals]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ValueError("manual batch contains duplicate semantic proposals")

    record = CatalogGenerationRecord(
        generation_id=request.generation_id,
        connection_id=request.connection_id,
        provider_id=provider_result.provider_id,
        prompt_template_version=provider_result.prompt_template_version,
        schema_fingerprint=request.snapshot.fingerprint,
        input_fingerprint=input_fingerprint,
        proposal_ids=proposal_ids,
        created_by=provider_result.created_by,
        created_at=provider_result.created_at,
    )
    raw = store.to_dict()
    raw.setdefault("draft_proposals", []).extend(
        proposal.model_dump(mode="json", exclude_none=True, round_trip=True)
        for proposal in proposals
    )
    raw.setdefault("generation_records", []).append(
        record.model_dump(mode="json", exclude_none=True)
    )
    return CatalogGenerationUpdate(
        store=store.replace(raw),
        outcome="generated",
        generation_id=request.generation_id,
        added_proposal_ids=tuple(proposal_ids),
    )
