"""32B-1 governed review, edit, approve/reject, publish, and rollback.

Every function here is a pure transformation `(CatalogStore) -> CatalogStore`
(wrapped in a `CatalogGovernanceUpdate`), driven through the same
`CatalogFileRepository` lock and atomic write 32A already built for schema
refresh and draft generation (see `catalog/repository.py`) — there is no
second catalog file, database, or mutation path.

A draft proposal can never publish itself: `publish_proposal` requires
`review_status == APPROVED`, set only by a separate `approve_proposal` call
attributed to an actor, and the merge into a real table/column/relationship
entry goes through the exact 32A-1 precedence gate
(`catalog.models.replacement_decision`) that already refuses to let a
non-verified source overwrite human-verified knowledge. A draft's content
model (`CatalogDraftContent`) structurally has no `sensitivity`,
`allow_samples`, policy, or mandatory-filter field, so publishing cannot
touch any of those regardless of what a reviewer approves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

import pydantic as pyd

from querygate.catalog.loader import CatalogStore, stable_entry_id
from querygate.catalog.models import (
    CatalogDraftContent,
    CatalogDraftObjectType,
    CatalogDraftProposal,
    CatalogDraftTarget,
    CatalogEntryProvenance,
    CatalogEntryStatus,
    CatalogVersionChange,
    CatalogVersionRecord,
    ProposalReviewStatus,
    replacement_decision,
)
from querygate.core.exceptions import CatalogGovernanceError, NotFoundError
from querygate.policy.models import Policy

_BULK_LIMIT = 50
_CONTENT_FIELDS = ("description", "aliases", "default_aggregation")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CatalogGovernanceUpdate:
    store: CatalogStore
    outcome: str
    proposal_id: Optional[str] = None
    proposal_ids: tuple[str, ...] = ()
    version_id: Optional[str] = None
    entry_id: Optional[str] = None
    rolled_back_version_id: Optional[str] = None


class ProposalPublishPreview(pyd.BaseModel):
    """Read-only, test-as-principal simulation of what publishing would do.

    Never persists anything and never returns draft content to a caller
    whose policy would hide the target object — visibility is evaluated
    first, exactly like `describe_table`/`search_catalog`.
    """

    proposal_id: str
    visible_to_principal: bool
    would_conflict: bool = False
    conflicting_fields: tuple[str, ...] = ()
    change_kind: Optional[Literal["created", "updated"]] = None

    model_config = pyd.ConfigDict(extra="forbid")


@dataclass(frozen=True)
class _PublishPlan:
    target: CatalogDraftTarget
    entry_id: str
    change_kind: Literal["created", "updated"]
    before: Optional[CatalogDraftContent]
    merged_fields: dict = field(default_factory=dict)
    table_key: str = ""
    conflicts: tuple[str, ...] = ()


def _casefold_lookup(mapping: dict, key: str) -> Optional[str]:
    target = key.casefold()
    for existing_key in mapping:
        if existing_key.casefold() == target:
            return existing_key
    return None


def _existing_field_values(entity_raw: Optional[dict]) -> dict:
    if entity_raw is None:
        return {}
    values: dict = {}
    if entity_raw.get("description"):
        values["description"] = entity_raw["description"]
    if entity_raw.get("aliases"):
        values["aliases"] = list(entity_raw["aliases"])
    if entity_raw.get("default_aggregation"):
        values["default_aggregation"] = entity_raw["default_aggregation"]
    return values


def _touched_fields(content: CatalogDraftContent) -> list[str]:
    fields: list[str] = []
    if content.description is not None:
        fields.append("description")
    if content.aliases:
        fields.append("aliases")
    if content.default_aggregation is not None:
        fields.append("default_aggregation")
    return fields


def _resolve_entity(
    store: CatalogStore, table_raw: Optional[dict], target: CatalogDraftTarget
) -> tuple[Optional[dict], Optional[CatalogEntryProvenance], str]:
    connection_id = target.connection_id
    table_model = store.get_table(connection_id, target.table)

    if target.object_type == CatalogDraftObjectType.TABLE:
        entry_id = stable_entry_id(connection_id, "table", target.table)
        return table_raw, (table_model.provenance if table_model else None), entry_id

    if target.object_type == CatalogDraftObjectType.COLUMN:
        entry_id = stable_entry_id(connection_id, "table", target.table, "column", target.column)
        columns_raw = (table_raw or {}).get("columns", {}) or {}
        column_key = _casefold_lookup(columns_raw, target.column)
        entity_raw = columns_raw.get(column_key) if column_key else None
        column_model = table_model.column(target.column) if table_model else None
        return entity_raw, (column_model.provenance if column_model else None), entry_id

    # RELATIONSHIP
    entry_id = stable_entry_id(
        connection_id,
        "table",
        target.table,
        "relationship",
        target.column,
        target.to_table,
        target.to_column,
    )
    relationships_raw = (table_raw or {}).get("relationships", []) or []

    def _matches(candidate: dict) -> bool:
        return (
            candidate.get("column", "").casefold() == target.column.casefold()
            and candidate.get("to_table", "").casefold() == target.to_table.casefold()
            and candidate.get("to_column", "").casefold() == target.to_column.casefold()
        )

    entity_raw = next((r for r in relationships_raw if _matches(r)), None)
    existing_provenance = None
    if table_model is not None:
        for relationship in table_model.relationships:
            if (
                relationship.column.casefold(),
                relationship.to_table.casefold(),
                relationship.to_column.casefold(),
            ) == (
                target.column.casefold(),
                target.to_table.casefold(),
                target.to_column.casefold(),
            ):
                existing_provenance = relationship.provenance
                break
    return entity_raw, existing_provenance, entry_id


def _plan_publish(store: CatalogStore, proposal: CatalogDraftProposal) -> _PublishPlan:
    target = proposal.target
    connection_id = target.connection_id
    raw = store.to_dict()
    tables_raw = raw.get("connections", {}).get(connection_id, {}).get("tables", {}) or {}
    table_key = _casefold_lookup(tables_raw, target.table) or target.table
    table_raw = tables_raw.get(table_key)

    entity_raw, existing_provenance, entry_id = _resolve_entity(store, table_raw, target)
    existing_values = _existing_field_values(entity_raw)
    touched = _touched_fields(proposal.content)

    # A minimal, always-valid probe provenance — replacement_decision only
    # inspects source_class/status/precedence, none of which depend on the
    # real actor/timestamp, so planning stays actor-agnostic and safe to
    # reuse for a read-only preview.
    probe_provenance = CatalogEntryProvenance(source_class="verified", status="verified")

    merged_fields: dict = {}
    conflicts: list[str] = []
    for candidate_field in touched:
        new_value = getattr(proposal.content, candidate_field)
        old_value = existing_values.get(candidate_field)
        if candidate_field == "aliases":
            changed = sorted(old_value or []) != sorted(new_value or [])
        else:
            changed = old_value != new_value
        if existing_provenance is not None:
            # `replacement_decision` (with a VERIFIED probe, the only shape a
            # governed publish is ever allowed to present) always lets a
            # human-approved candidate replace non-verified content — that
            # check alone can't distinguish "fills a gap" from "silently
            # overwrites a value someone already verified". A publish must
            # never do the latter: an existing verified field that would
            # change is always a reviewable conflict, full stop, regardless
            # of precedence. Undo via rollback, or edit catalog.yaml
            # directly, are the sanctioned ways to change verified content.
            had_prior_value = candidate_field in existing_values
            if (
                existing_provenance.status == CatalogEntryStatus.VERIFIED
                and had_prior_value
                and changed
            ):
                conflicts.append(candidate_field)
                continue
            decision = replacement_decision(
                existing_provenance,
                probe_provenance,
                field_name=candidate_field,
                value_changes=changed,
            )
            if not decision.allowed:
                conflicts.append(candidate_field)
                continue
        merged_fields[candidate_field] = new_value

    # The entity *row* existing (even with none of these fields populated
    # yet) still means rollback must clear fields, not delete the row —
    # only a genuinely absent entity is a "created" change for rollback
    # purposes. `before` may legitimately be None even for an "updated"
    # change: that just means these specific fields had no prior value.
    before = None
    before_kwargs = {name: existing_values[name] for name in touched if name in existing_values}
    if before_kwargs:
        before = CatalogDraftContent(**before_kwargs)

    return _PublishPlan(
        target=target,
        entry_id=entry_id,
        change_kind="updated" if entity_raw is not None else "created",
        before=before,
        merged_fields=merged_fields,
        table_key=table_key,
        conflicts=tuple(conflicts),
    )


def _require_proposal(
    store: CatalogStore, proposal_id: str, connection_id: str
) -> CatalogDraftProposal:
    proposal = store.get_draft_proposal(proposal_id)
    if proposal is None or proposal.target.connection_id != connection_id:
        raise NotFoundError(f"Unknown catalog proposal: {proposal_id!r}")
    return proposal


def _find_proposal_raw(raw: dict, proposal_id: str) -> dict:
    for proposal_raw in raw.get("draft_proposals", []):
        if proposal_raw.get("proposal_id") == proposal_id:
            return proposal_raw
    raise NotFoundError(f"Unknown catalog proposal: {proposal_id!r}")


def edit_proposal(
    store: CatalogStore,
    *,
    proposal_id: str,
    connection_id: str,
    actor: str,
    content: CatalogDraftContent,
    now: Optional[datetime] = None,
) -> CatalogGovernanceUpdate:
    now = now or _utcnow()
    proposal = _require_proposal(store, proposal_id, connection_id)
    if proposal.review_status != ProposalReviewStatus.PENDING:
        raise CatalogGovernanceError(
            f"proposal {proposal_id!r} can only be edited while pending "
            f"(current status: {proposal.review_status.value!r})"
        )
    if (
        proposal.target.object_type != CatalogDraftObjectType.TABLE
        and content.default_aggregation is not None
    ):
        raise CatalogGovernanceError("default_aggregation may only be proposed for a table")
    if proposal.target.object_type == CatalogDraftObjectType.RELATIONSHIP and content.aliases:
        raise CatalogGovernanceError("relationship proposals cannot define aliases")

    raw = store.to_dict()
    proposal_raw = _find_proposal_raw(raw, proposal_id)
    proposal_raw["content"] = content.model_dump(mode="json", exclude_none=True)
    proposal_raw.setdefault("review_history", []).append(
        {"action": "edited", "actor": actor, "occurred_at": now.isoformat()}
    )
    return CatalogGovernanceUpdate(
        store=store.replace(raw), outcome="edited", proposal_id=proposal_id
    )


def approve_proposal(
    store: CatalogStore,
    *,
    proposal_id: str,
    connection_id: str,
    actor: str,
    now: Optional[datetime] = None,
) -> CatalogGovernanceUpdate:
    now = now or _utcnow()
    proposal = _require_proposal(store, proposal_id, connection_id)
    if proposal.review_status != ProposalReviewStatus.PENDING:
        raise CatalogGovernanceError(
            f"proposal {proposal_id!r} can only be approved while pending "
            f"(current status: {proposal.review_status.value!r})"
        )
    if proposal.provenance.status == CatalogEntryStatus.STALE:
        raise CatalogGovernanceError(
            f"proposal {proposal_id!r} is stale relative to the current schema and cannot be approved"
        )

    raw = store.to_dict()
    proposal_raw = _find_proposal_raw(raw, proposal_id)
    proposal_raw["review_status"] = "approved"
    proposal_raw.setdefault("review_history", []).append(
        {"action": "approved", "actor": actor, "occurred_at": now.isoformat()}
    )
    return CatalogGovernanceUpdate(
        store=store.replace(raw), outcome="approved", proposal_id=proposal_id
    )


def reject_proposal(
    store: CatalogStore,
    *,
    proposal_id: str,
    connection_id: str,
    actor: str,
    reason: str,
    now: Optional[datetime] = None,
) -> CatalogGovernanceUpdate:
    now = now or _utcnow()
    if not reason or not reason.strip():
        raise CatalogGovernanceError("a rejection reason is required")
    proposal = _require_proposal(store, proposal_id, connection_id)
    if proposal.review_status not in {ProposalReviewStatus.PENDING, ProposalReviewStatus.APPROVED}:
        raise CatalogGovernanceError(
            f"proposal {proposal_id!r} cannot be rejected from status "
            f"{proposal.review_status.value!r}"
        )

    raw = store.to_dict()
    proposal_raw = _find_proposal_raw(raw, proposal_id)
    proposal_raw["review_status"] = "rejected"
    proposal_raw.setdefault("review_history", []).append(
        {
            "action": "rejected",
            "actor": actor,
            "occurred_at": now.isoformat(),
            "reason": reason.strip(),
        }
    )
    return CatalogGovernanceUpdate(
        store=store.replace(raw), outcome="rejected", proposal_id=proposal_id
    )


def _bulk_precheck(
    store: CatalogStore, proposal_ids: tuple[str, ...], connection_id: str, *, allowed_from: set
) -> None:
    if not proposal_ids:
        raise CatalogGovernanceError("a bulk operation requires at least one proposal id")
    if len(proposal_ids) > _BULK_LIMIT:
        raise CatalogGovernanceError(
            f"bulk operations are limited to {_BULK_LIMIT} proposals per call"
        )
    if len(set(proposal_ids)) != len(proposal_ids):
        raise CatalogGovernanceError("bulk operation proposal ids must be unique")
    for proposal_id in proposal_ids:
        proposal = _require_proposal(store, proposal_id, connection_id)
        if proposal.review_status not in allowed_from:
            raise CatalogGovernanceError(
                f"proposal {proposal_id!r} cannot transition from status "
                f"{proposal.review_status.value!r}"
            )


def bulk_approve_proposals(
    store: CatalogStore,
    *,
    proposal_ids: tuple[str, ...],
    connection_id: str,
    actor: str,
    now: Optional[datetime] = None,
) -> CatalogGovernanceUpdate:
    now = now or _utcnow()
    _bulk_precheck(store, proposal_ids, connection_id, allowed_from={ProposalReviewStatus.PENDING})
    for proposal_id in proposal_ids:
        proposal = store.get_draft_proposal(proposal_id)
        if proposal is not None and proposal.provenance.status == CatalogEntryStatus.STALE:
            raise CatalogGovernanceError(
                f"proposal {proposal_id!r} is stale relative to the current schema and cannot be approved"
            )

    raw = store.to_dict()
    for proposal_id in proposal_ids:
        proposal_raw = _find_proposal_raw(raw, proposal_id)
        proposal_raw["review_status"] = "approved"
        proposal_raw.setdefault("review_history", []).append(
            {"action": "approved", "actor": actor, "occurred_at": now.isoformat()}
        )
    return CatalogGovernanceUpdate(
        store=store.replace(raw), outcome="bulk_approved", proposal_ids=proposal_ids
    )


def bulk_reject_proposals(
    store: CatalogStore,
    *,
    proposal_ids: tuple[str, ...],
    connection_id: str,
    actor: str,
    reason: str,
    now: Optional[datetime] = None,
) -> CatalogGovernanceUpdate:
    now = now or _utcnow()
    if not reason or not reason.strip():
        raise CatalogGovernanceError("a rejection reason is required")
    _bulk_precheck(
        store,
        proposal_ids,
        connection_id,
        allowed_from={ProposalReviewStatus.PENDING, ProposalReviewStatus.APPROVED},
    )

    raw = store.to_dict()
    for proposal_id in proposal_ids:
        proposal_raw = _find_proposal_raw(raw, proposal_id)
        proposal_raw["review_status"] = "rejected"
        proposal_raw.setdefault("review_history", []).append(
            {
                "action": "rejected",
                "actor": actor,
                "occurred_at": now.isoformat(),
                "reason": reason.strip(),
            }
        )
    return CatalogGovernanceUpdate(
        store=store.replace(raw), outcome="bulk_rejected", proposal_ids=proposal_ids
    )


def preview_publish(
    store: CatalogStore, *, proposal_id: str, connection_id: str, policy: Policy
) -> ProposalPublishPreview:
    """Test-as-principal dry run: never mutates the store, never returns
    content the given policy would hide (mirrors ``describe_table``'s own
    table/column visibility check).
    """

    proposal = _require_proposal(store, proposal_id, connection_id)
    target = proposal.target
    visible = policy.table_allowed(target.table)
    if visible and target.object_type == CatalogDraftObjectType.COLUMN:
        visible = policy.column_allowed(target.table, target.column)
    if visible and target.object_type == CatalogDraftObjectType.RELATIONSHIP:
        visible = (
            policy.table_allowed(target.to_table)
            and policy.column_allowed(target.table, target.column)
            and policy.column_allowed(target.to_table, target.to_column)
        )
    if not visible:
        return ProposalPublishPreview(proposal_id=proposal_id, visible_to_principal=False)

    plan = _plan_publish(store, proposal)
    return ProposalPublishPreview(
        proposal_id=proposal_id,
        visible_to_principal=True,
        would_conflict=bool(plan.conflicts),
        conflicting_fields=plan.conflicts,
        change_kind=None if plan.conflicts else plan.change_kind,
    )


def publish_proposal(
    store: CatalogStore,
    *,
    proposal_id: str,
    connection_id: str,
    actor: str,
    now: Optional[datetime] = None,
) -> CatalogGovernanceUpdate:
    now = now or _utcnow()
    proposal = _require_proposal(store, proposal_id, connection_id)
    if proposal.review_status != ProposalReviewStatus.APPROVED:
        raise CatalogGovernanceError(
            f"proposal {proposal_id!r} must be approved before publishing "
            f"(current status: {proposal.review_status.value!r})"
        )
    if proposal.provenance.status == CatalogEntryStatus.STALE:
        raise CatalogGovernanceError(
            f"proposal {proposal_id!r} is stale relative to the current schema and cannot be published"
        )
    snapshot = store.get_schema_snapshot(connection_id)
    if snapshot is not None and proposal.provenance.schema_fingerprint != snapshot.fingerprint:
        raise CatalogGovernanceError(
            f"proposal {proposal_id!r} was generated against schema fingerprint "
            f"{proposal.provenance.schema_fingerprint!r}, current is {snapshot.fingerprint!r}; "
            "refresh and regenerate before publishing"
        )

    plan = _plan_publish(store, proposal)
    if plan.conflicts:
        raise CatalogGovernanceError(
            f"proposal {proposal_id!r} conflicts with already-verified content on field(s) "
            f"{', '.join(plan.conflicts)} — edit the proposal or resolve the conflict manually"
        )
    if not plan.merged_fields:
        raise CatalogGovernanceError(f"proposal {proposal_id!r} has no fields left to publish")

    raw = store.to_dict()
    connection_raw = raw.setdefault("connections", {}).setdefault(connection_id, {"tables": {}})
    tables_raw = connection_raw.setdefault("tables", {})
    table_raw = tables_raw.get(plan.table_key)
    if table_raw is None:
        table_raw = {"columns": {}, "relationships": []}
        tables_raw[plan.table_key] = table_raw

    fingerprint = snapshot.fingerprint if snapshot is not None else "untracked"
    provenance_dict = {
        "entry_id": plan.entry_id,
        "catalog_version": store.version,
        "source_class": "verified",
        "source_evidence": [{"kind": "import", "reference": f"proposal:{proposal_id}"}],
        "confidence": 1.0,
        "status": "verified",
        "schema_fingerprint": fingerprint,
        "created_by": proposal.provenance.created_by,
        "approved_by": actor,
        "created_at": (
            proposal.provenance.created_at.isoformat() if proposal.provenance.created_at else None
        ),
        "approved_at": now.isoformat(),
    }

    target = proposal.target
    if target.object_type == CatalogDraftObjectType.TABLE:
        for content_field, value in plan.merged_fields.items():
            table_raw[content_field] = value
        table_raw["provenance"] = provenance_dict
    elif target.object_type == CatalogDraftObjectType.COLUMN:
        columns_raw = table_raw.setdefault("columns", {})
        column_key = _casefold_lookup(columns_raw, target.column) or target.column
        column_raw = columns_raw.get(column_key, {})
        for content_field, value in plan.merged_fields.items():
            column_raw[content_field] = value
        column_raw["provenance"] = provenance_dict
        columns_raw[column_key] = column_raw
    else:
        relationships_raw = table_raw.setdefault("relationships", [])
        existing_index = next(
            (
                index
                for index, candidate in enumerate(relationships_raw)
                if candidate.get("column", "").casefold() == target.column.casefold()
                and candidate.get("to_table", "").casefold() == target.to_table.casefold()
                and candidate.get("to_column", "").casefold() == target.to_column.casefold()
            ),
            None,
        )
        relationship_raw = relationships_raw[existing_index] if existing_index is not None else {}
        relationship_raw["column"] = target.column
        relationship_raw["to_table"] = target.to_table
        relationship_raw["to_column"] = target.to_column
        for content_field, value in plan.merged_fields.items():
            relationship_raw[content_field] = value
        relationship_raw["provenance"] = provenance_dict
        if existing_index is not None:
            relationships_raw[existing_index] = relationship_raw
        else:
            relationships_raw.append(relationship_raw)

    version_id = str(len(raw.get("version_history", [])) + 1)
    change = CatalogVersionChange(
        target=target,
        change=plan.change_kind,
        before=plan.before,
        after=CatalogDraftContent(
            **{
                content_field: getattr(proposal.content, content_field)
                for content_field in plan.merged_fields
            }
        ),
    )
    version_record = CatalogVersionRecord(
        version_id=version_id,
        action="publish",
        status="active",
        actor=actor,
        occurred_at=now,
        proposal_id=proposal_id,
        changes=[change],
    )
    raw.setdefault("version_history", []).append(
        version_record.model_dump(mode="json", exclude_none=True)
    )

    proposal_raw = _find_proposal_raw(raw, proposal_id)
    proposal_raw["review_status"] = "published"
    proposal_raw["published_entry_id"] = plan.entry_id
    proposal_raw["published_version_id"] = version_id
    proposal_raw.setdefault("review_history", []).append(
        {"action": "published", "actor": actor, "occurred_at": now.isoformat()}
    )

    return CatalogGovernanceUpdate(
        store=store.replace(raw),
        outcome="published",
        proposal_id=proposal_id,
        version_id=version_id,
        entry_id=plan.entry_id,
    )


def rollback_version(
    store: CatalogStore,
    *,
    version_id: str,
    connection_id: str,
    actor: str,
    now: Optional[datetime] = None,
) -> CatalogGovernanceUpdate:
    now = now or _utcnow()
    record = store.get_version_record(version_id)
    if record is None:
        raise NotFoundError(f"Unknown catalog version: {version_id!r}")
    if record.action.value != "publish":
        raise CatalogGovernanceError("only a publish version can be rolled back")
    if record.status.value != "active":
        raise CatalogGovernanceError(f"version {version_id!r} has already been reverted")

    change = record.changes[0]
    target = change.target
    if target.connection_id != connection_id:
        raise NotFoundError(f"Unknown catalog version: {version_id!r}")

    raw = store.to_dict()
    tables_raw = raw.get("connections", {}).get(connection_id, {}).get("tables", {}) or {}
    table_key = _casefold_lookup(tables_raw, target.table)
    table_raw = tables_raw.get(table_key) if table_key else None
    if table_raw is None:
        raise CatalogGovernanceError("the published table no longer exists; rollback refused")

    entity_raw, current_provenance, expected_entry_id = _resolve_entity(store, table_raw, target)
    if entity_raw is None or current_provenance is None:
        raise CatalogGovernanceError("the published entry no longer exists; rollback refused")
    if (
        current_provenance.entry_id != expected_entry_id
        or current_provenance.approved_at != record.occurred_at
    ):
        raise CatalogGovernanceError(
            "the entry has changed since this version was published; rollback refused to "
            "avoid discarding a newer change"
        )

    if change.change == "created":
        if target.object_type == CatalogDraftObjectType.TABLE:
            if table_raw.get("columns") or table_raw.get("relationships"):
                raise CatalogGovernanceError(
                    "cannot roll back a table creation while the table still has columns or "
                    "relationships; roll those back first"
                )
            del tables_raw[table_key]
        elif target.object_type == CatalogDraftObjectType.COLUMN:
            columns_raw = table_raw.get("columns", {}) or {}
            column_key = _casefold_lookup(columns_raw, target.column)
            if column_key:
                del columns_raw[column_key]
        else:
            relationships_raw = table_raw.get("relationships", []) or []
            table_raw["relationships"] = [
                candidate
                for candidate in relationships_raw
                if not (
                    candidate.get("column", "").casefold() == target.column.casefold()
                    and candidate.get("to_table", "").casefold() == target.to_table.casefold()
                    and candidate.get("to_column", "").casefold() == target.to_column.casefold()
                )
            ]
        reverse_change = CatalogVersionChange(target=target, change="removed", before=change.after)
    else:
        restored = change.before.model_dump(mode="json", exclude_none=True) if change.before else {}
        for content_field in _CONTENT_FIELDS:
            entity_raw.pop(content_field, None)
        entity_raw.update(restored)
        reverse_change = CatalogVersionChange(
            target=target, change="updated", before=change.after, after=change.before
        )

    new_version_id = str(len(raw.get("version_history", [])) + 1)
    rollback_record = CatalogVersionRecord(
        version_id=new_version_id,
        action="rollback",
        status="active",
        actor=actor,
        occurred_at=now,
        rolled_back_version_id=version_id,
        changes=[reverse_change],
    )
    raw.setdefault("version_history", []).append(
        rollback_record.model_dump(mode="json", exclude_none=True)
    )
    for existing_record in raw.get("version_history", []):
        if existing_record.get("version_id") == version_id:
            existing_record["status"] = "reverted"

    if record.proposal_id is not None:
        try:
            proposal_raw = _find_proposal_raw(raw, record.proposal_id)
        except NotFoundError:
            proposal_raw = None
        if proposal_raw is not None:
            proposal_raw.setdefault("review_history", []).append(
                {"action": "rolled_back", "actor": actor, "occurred_at": now.isoformat()}
            )

    return CatalogGovernanceUpdate(
        store=store.replace(raw),
        outcome="rolled_back",
        version_id=new_version_id,
        rolled_back_version_id=version_id,
    )
