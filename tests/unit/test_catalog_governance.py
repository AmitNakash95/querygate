"""32B-1 governed review/edit/approve/reject/publish/rollback tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from querygate.catalog import governance
from querygate.catalog.generation import generate_catalog_drafts
from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import CatalogDraftContent, ProposalReviewStatus
from querygate.catalog.providers import ManualDraftBatch, ManualSemanticMemoryProvider
from querygate.catalog.providers import SemanticGenerationRequest
from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate
from querygate.catalog.retrieval import search_catalog
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.core.exceptions import CatalogGovernanceError, NotFoundError
from querygate.policy.models import Policy

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot() -> ObservedSchemaSnapshot:
    metadata = sa.MetaData()
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id")),
    )
    return ObservedSchemaSnapshot.from_tables("demo", [customers, orders])


def _base_store(
    *, verified_description: str | None = "Verified customer accounts."
) -> CatalogStore:
    snapshot = _snapshot()
    tables = {"customers": {"description": verified_description}} if verified_description else {}
    return CatalogStore.from_dict(
        {
            "version": 2,
            "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
            "connections": {"demo": {"tables": tables}},
        }
    )


def _batch(*, generation_id: str = "onboarding-1", table_description: str = "Customer entity."):
    snapshot = _snapshot()
    return ManualDraftBatch.model_validate(
        {
            "generation_id": generation_id,
            "connection_id": "demo",
            "schema_fingerprint": snapshot.fingerprint,
            "created_by": "offline-operator",
            "suggestions": [
                {
                    "target": {
                        "connection_id": "demo",
                        "object_type": "table",
                        "table": "customers",
                    },
                    "content": {"description": table_description, "aliases": ["accounts"]},
                    "confidence": 0.8,
                },
                {
                    "target": {
                        "connection_id": "demo",
                        "object_type": "column",
                        "table": "customers",
                        "column": "email",
                    },
                    "content": {"description": "Customer email address."},
                    "confidence": 0.75,
                },
            ],
        }
    )


def _store_with_pending_proposals(**batch_kwargs) -> CatalogStore:
    store = _base_store()
    request = SemanticGenerationRequest(
        generation_id=batch_kwargs.get("generation_id", "onboarding-1"),
        connection_id="demo",
        snapshot=_snapshot(),
    )
    update = generate_catalog_drafts(
        store, request=request, provider=ManualSemanticMemoryProvider(_batch(**batch_kwargs))
    )
    return update.store


def _table_proposal_id(store: CatalogStore) -> str:
    return next(
        p.proposal_id
        for p in store.iter_draft_proposals("demo")
        if p.target.object_type.value == "table"
    )


def _column_proposal_id(store: CatalogStore) -> str:
    return next(
        p.proposal_id
        for p in store.iter_draft_proposals("demo")
        if p.target.object_type.value == "column"
    )


# ---------------------------------------------------------------------------
# Approve / reject / edit state machine
# ---------------------------------------------------------------------------


def test_new_proposal_starts_pending():
    store = _store_with_pending_proposals()
    for proposal in store.iter_draft_proposals("demo"):
        assert proposal.review_status == ProposalReviewStatus.PENDING
        assert proposal.review_history == []


def test_approve_then_reject_is_rejected_from_wrong_state():
    store = _store_with_pending_proposals()
    proposal_id = _table_proposal_id(store)

    update = governance.approve_proposal(
        store, proposal_id=proposal_id, connection_id="demo", actor="reviewer-a", now=_NOW
    )
    assert (
        update.store.get_draft_proposal(proposal_id).review_status == ProposalReviewStatus.APPROVED
    )

    # Repeating approve on an already-approved proposal must fail cleanly.
    with pytest.raises(CatalogGovernanceError, match="pending"):
        governance.approve_proposal(
            update.store,
            proposal_id=proposal_id,
            connection_id="demo",
            actor="reviewer-b",
            now=_NOW,
        )


def test_reject_requires_a_reason_and_is_allowed_from_pending_or_approved():
    store = _store_with_pending_proposals()
    proposal_id = _table_proposal_id(store)

    with pytest.raises(CatalogGovernanceError, match="reason"):
        governance.reject_proposal(
            store,
            proposal_id=proposal_id,
            connection_id="demo",
            actor="reviewer-a",
            reason="   ",
            now=_NOW,
        )

    approved = governance.approve_proposal(
        store, proposal_id=proposal_id, connection_id="demo", actor="reviewer-a", now=_NOW
    ).store
    rejected = governance.reject_proposal(
        approved,
        proposal_id=proposal_id,
        connection_id="demo",
        actor="reviewer-b",
        reason="not accurate",
        now=_NOW,
    ).store
    proposal = rejected.get_draft_proposal(proposal_id)
    assert proposal.review_status == ProposalReviewStatus.REJECTED
    assert proposal.review_history[-1].action.value == "rejected"
    assert proposal.review_history[-1].reason == "not accurate"

    # Rejecting an already-rejected proposal must fail without mutation.
    with pytest.raises(CatalogGovernanceError):
        governance.reject_proposal(
            rejected,
            proposal_id=proposal_id,
            connection_id="demo",
            actor="x",
            reason="again",
            now=_NOW,
        )


def test_edit_only_allowed_while_pending_and_validates_shape():
    store = _store_with_pending_proposals()
    proposal_id = _table_proposal_id(store)

    edited = governance.edit_proposal(
        store,
        proposal_id=proposal_id,
        connection_id="demo",
        actor="reviewer-a",
        content=CatalogDraftContent(description="Better description."),
        now=_NOW,
    ).store
    proposal = edited.get_draft_proposal(proposal_id)
    assert proposal.content.description == "Better description."
    assert proposal.review_history[-1].action.value == "edited"

    approved = governance.approve_proposal(
        edited, proposal_id=proposal_id, connection_id="demo", actor="reviewer-a", now=_NOW
    ).store
    with pytest.raises(CatalogGovernanceError, match="pending"):
        governance.edit_proposal(
            approved,
            proposal_id=proposal_id,
            connection_id="demo",
            actor="reviewer-a",
            content=CatalogDraftContent(description="Too late."),
            now=_NOW,
        )

    column_id = _column_proposal_id(store)
    with pytest.raises(CatalogGovernanceError, match="default_aggregation"):
        governance.edit_proposal(
            store,
            proposal_id=column_id,
            connection_id="demo",
            actor="reviewer-a",
            content=CatalogDraftContent(default_aggregation="count(*)"),
            now=_NOW,
        )


def test_unknown_or_cross_connection_proposal_raises_not_found():
    store = _store_with_pending_proposals()
    with pytest.raises(NotFoundError):
        governance.approve_proposal(
            store, proposal_id="does-not-exist", connection_id="demo", actor="x", now=_NOW
        )
    proposal_id = _table_proposal_id(store)
    with pytest.raises(NotFoundError):
        governance.approve_proposal(
            store, proposal_id=proposal_id, connection_id="other-connection", actor="x", now=_NOW
        )


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------


def test_bulk_reject_is_atomic_all_or_nothing():
    store = _store_with_pending_proposals()
    table_id = _table_proposal_id(store)
    column_id = _column_proposal_id(store)

    with pytest.raises(NotFoundError):
        governance.bulk_reject_proposals(
            store,
            proposal_ids=(table_id, "unknown-id"),
            connection_id="demo",
            actor="reviewer",
            reason="bad batch",
            now=_NOW,
        )
    # No partial mutation: the valid id must remain untouched.
    assert store.get_draft_proposal(table_id).review_status == ProposalReviewStatus.PENDING

    update = governance.bulk_reject_proposals(
        store,
        proposal_ids=(table_id, column_id),
        connection_id="demo",
        actor="reviewer",
        reason="bad batch",
        now=_NOW,
    )
    assert set(update.proposal_ids) == {table_id, column_id}
    for proposal_id in (table_id, column_id):
        assert (
            update.store.get_draft_proposal(proposal_id).review_status
            == ProposalReviewStatus.REJECTED
        )


def test_bulk_operation_rejects_duplicate_ids_and_oversized_batches():
    store = _store_with_pending_proposals()
    table_id = _table_proposal_id(store)
    with pytest.raises(CatalogGovernanceError, match="unique"):
        governance.bulk_approve_proposals(
            store, proposal_ids=(table_id, table_id), connection_id="demo", actor="x", now=_NOW
        )
    with pytest.raises(CatalogGovernanceError, match="limited"):
        governance.bulk_approve_proposals(
            store,
            proposal_ids=tuple(f"id-{i}" for i in range(51)),
            connection_id="demo",
            actor="x",
            now=_NOW,
        )


# ---------------------------------------------------------------------------
# Publish: creation, conflicts, staleness
# ---------------------------------------------------------------------------


def _approved_store(store: CatalogStore, proposal_id: str) -> CatalogStore:
    return governance.approve_proposal(
        store, proposal_id=proposal_id, connection_id="demo", actor="reviewer", now=_NOW
    ).store


def test_publish_requires_approved_status():
    store = _store_with_pending_proposals()
    proposal_id = _table_proposal_id(store)
    with pytest.raises(CatalogGovernanceError, match="approved"):
        governance.publish_proposal(
            store, proposal_id=proposal_id, connection_id="demo", actor="publisher", now=_NOW
        )


def test_publish_creates_a_new_verified_column_entry():
    store = _store_with_pending_proposals()
    column_id = _column_proposal_id(store)
    approved = _approved_store(store, column_id)

    update = governance.publish_proposal(
        store=approved, proposal_id=column_id, connection_id="demo", actor="publisher", now=_NOW
    )
    assert update.outcome == "published"
    assert update.version_id == "1"

    table = update.store.get_table("demo", "customers")
    column = table.column("email")
    assert column.description == "Customer email address."
    assert column.provenance.source_class.value == "verified"
    assert column.provenance.status.value == "verified"
    assert column.provenance.approved_by == "publisher"
    assert column.provenance.entry_id == update.entry_id

    proposal = update.store.get_draft_proposal(column_id)
    assert proposal.review_status == ProposalReviewStatus.PUBLISHED
    assert proposal.published_entry_id == update.entry_id
    assert proposal.published_version_id == "1"

    record = update.store.get_version_record("1")
    assert record.action.value == "publish"
    assert record.changes[0].change == "created"
    assert record.changes[0].before is None

    # Published content is now genuinely agent-visible through ordinary search.
    hits = search_catalog(
        update.store, connection_id="demo", policy=Policy(), query="email address"
    )
    assert any(hit.column == "email" for hit in hits.results)


def test_publish_conflicts_with_already_verified_content_and_mutates_nothing():
    store = _store_with_pending_proposals(
        table_description="A different description than verified."
    )
    table_id = _table_proposal_id(store)
    approved = _approved_store(store, table_id)

    with pytest.raises(CatalogGovernanceError, match="conflict"):
        governance.publish_proposal(
            store=approved, proposal_id=table_id, connection_id="demo", actor="publisher", now=_NOW
        )
    # No mutation: still verified original description, no version history.
    assert approved.get_table("demo", "customers").description == "Verified customer accounts."
    assert list(approved.iter_version_history("demo")) == []


def test_publish_allows_identical_content_without_conflict():
    store = _store_with_pending_proposals(table_description="Verified customer accounts.")
    table_id = _table_proposal_id(store)
    approved = _approved_store(store, table_id)

    update = governance.publish_proposal(
        store=approved, proposal_id=table_id, connection_id="demo", actor="publisher", now=_NOW
    )
    assert update.outcome == "published"


def test_publish_rejects_stale_proposal_and_fingerprint_mismatch():
    store = _store_with_pending_proposals()
    table_id = _table_proposal_id(store)
    approved = _approved_store(store, table_id)

    raw = approved.to_dict()
    for proposal_raw in raw["draft_proposals"]:
        if proposal_raw["proposal_id"] == table_id:
            proposal_raw["provenance"]["schema_fingerprint"] = "sha256:" + "0" * 64
    mismatched = approved.replace(raw)

    with pytest.raises(CatalogGovernanceError, match="fingerprint"):
        governance.publish_proposal(
            store=mismatched,
            proposal_id=table_id,
            connection_id="demo",
            actor="publisher",
            now=_NOW,
        )


def test_approve_rejects_a_stale_proposal():
    store = _store_with_pending_proposals()
    table_id = _table_proposal_id(store)
    raw = store.to_dict()
    for proposal_raw in raw["draft_proposals"]:
        if proposal_raw["proposal_id"] == table_id:
            proposal_raw["provenance"]["status"] = "stale"
    stale_store = store.replace(raw)

    with pytest.raises(CatalogGovernanceError, match="stale"):
        governance.approve_proposal(
            stale_store, proposal_id=table_id, connection_id="demo", actor="reviewer", now=_NOW
        )


# ---------------------------------------------------------------------------
# Preview (test-as-principal)
# ---------------------------------------------------------------------------


def test_preview_hides_content_a_policy_would_deny():
    store = _store_with_pending_proposals()
    column_id = _column_proposal_id(store)
    denied_policy = Policy(denied_columns={"customers": ["email"]})
    preview = governance.preview_publish(
        store, proposal_id=column_id, connection_id="demo", policy=denied_policy
    )
    assert preview.visible_to_principal is False
    assert preview.would_conflict is False


def test_preview_reports_conflicts_without_mutating_store():
    store = _store_with_pending_proposals(table_description="Conflicting description.")
    table_id = _table_proposal_id(store)
    approved = _approved_store(store, table_id)

    preview = governance.preview_publish(
        approved, proposal_id=table_id, connection_id="demo", policy=Policy()
    )
    assert preview.visible_to_principal is True
    assert preview.would_conflict is True
    assert "description" in preview.conflicting_fields
    assert approved.get_table("demo", "customers").description == "Verified customer accounts."


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_rollback_of_a_created_column_removes_it():
    store = _store_with_pending_proposals()
    column_id = _column_proposal_id(store)
    approved = _approved_store(store, column_id)
    published = governance.publish_proposal(
        store=approved, proposal_id=column_id, connection_id="demo", actor="publisher", now=_NOW
    ).store
    assert published.get_table("demo", "customers").column("email") is not None

    rolled_back = governance.rollback_version(
        published, version_id="1", connection_id="demo", actor="operator", now=_NOW
    )
    assert rolled_back.outcome == "rolled_back"
    assert rolled_back.store.get_table("demo", "customers").column("email") is None

    original = rolled_back.store.get_version_record("1")
    assert original.status.value == "reverted"
    reversal = rolled_back.store.get_version_record(rolled_back.version_id)
    assert reversal.action.value == "rollback"
    assert reversal.rolled_back_version_id == "1"


def test_rollback_of_an_updated_field_restores_the_prior_value():
    store = _store_with_pending_proposals(table_description="Verified customer accounts.")
    table_id = _table_proposal_id(store)
    approved = _approved_store(store, table_id)
    published = governance.publish_proposal(
        store=approved, proposal_id=table_id, connection_id="demo", actor="publisher", now=_NOW
    ).store
    assert published.get_table("demo", "customers").description == "Verified customer accounts."
    assert published.get_table("demo", "customers").aliases == ["accounts"]

    rolled_back = governance.rollback_version(
        published, version_id="1", connection_id="demo", actor="operator", now=_NOW
    ).store
    table = rolled_back.get_table("demo", "customers")
    assert table.description == "Verified customer accounts."
    assert table.aliases == []


def test_rollback_is_not_repeatable():
    store = _store_with_pending_proposals()
    column_id = _column_proposal_id(store)
    approved = _approved_store(store, column_id)
    published = governance.publish_proposal(
        store=approved, proposal_id=column_id, connection_id="demo", actor="publisher", now=_NOW
    ).store
    rolled_back = governance.rollback_version(
        published, version_id="1", connection_id="demo", actor="operator", now=_NOW
    ).store

    with pytest.raises(CatalogGovernanceError, match="already been reverted"):
        governance.rollback_version(
            rolled_back, version_id="1", connection_id="demo", actor="operator", now=_NOW
        )
    with pytest.raises(CatalogGovernanceError, match="only a publish version"):
        governance.rollback_version(
            rolled_back,
            version_id=rolled_back.get_version_record(
                next(
                    r.version_id
                    for r in rolled_back.iter_version_history("demo")
                    if r.action.value == "rollback"
                )
            ).version_id,
            connection_id="demo",
            actor="operator",
            now=_NOW,
        )


def test_rollback_refuses_when_entry_changed_since_publish():
    store = _store_with_pending_proposals()
    column_id = _column_proposal_id(store)
    approved = _approved_store(store, column_id)
    published = governance.publish_proposal(
        store=approved, proposal_id=column_id, connection_id="demo", actor="publisher", now=_NOW
    ).store

    # Simulate a later, unrelated verified edit to the same column outside
    # this proposal's lineage (e.g. a manual catalog.yaml edit).
    raw = published.to_dict()
    raw["connections"]["demo"]["tables"]["customers"]["columns"]["email"]["provenance"][
        "approved_at"
    ] = "2030-01-01T00:00:00Z"
    drifted = published.replace(raw)

    with pytest.raises(CatalogGovernanceError, match="changed since"):
        governance.rollback_version(
            drifted, version_id="1", connection_id="demo", actor="operator", now=_NOW
        )


def test_rollback_of_table_creation_blocked_while_columns_remain():
    store = _base_store(verified_description=None)
    request = SemanticGenerationRequest(
        generation_id="onboarding-1", connection_id="demo", snapshot=_snapshot()
    )
    update = generate_catalog_drafts(
        store, request=request, provider=ManualSemanticMemoryProvider(_batch())
    )
    store = update.store
    table_id = _table_proposal_id(store)
    column_id = _column_proposal_id(store)

    store = governance.publish_proposal(
        store=_approved_store(store, table_id),
        proposal_id=table_id,
        connection_id="demo",
        actor="publisher",
        now=_NOW,
    ).store
    store = governance.publish_proposal(
        store=governance.approve_proposal(
            store, proposal_id=column_id, connection_id="demo", actor="reviewer", now=_NOW
        ).store,
        proposal_id=column_id,
        connection_id="demo",
        actor="publisher",
        now=_NOW,
    ).store

    with pytest.raises(CatalogGovernanceError, match="still has columns"):
        governance.rollback_version(
            store, version_id="1", connection_id="demo", actor="op", now=_NOW
        )


# ---------------------------------------------------------------------------
# Repository-level persistence (same lock 32A already uses)
# ---------------------------------------------------------------------------


def test_full_lifecycle_persists_through_the_catalog_file_repository(tmp_path):
    import yaml

    snapshot = _snapshot()
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "connections": {"demo": {"tables": {}}},
                "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
            },
            sort_keys=False,
        )
    )
    repository = CatalogFileRepository(str(catalog_path))

    def _generate(store: CatalogStore) -> CatalogFileUpdate:
        request = SemanticGenerationRequest(
            generation_id="onboarding-1", connection_id="demo", snapshot=snapshot
        )
        result = generate_catalog_drafts(
            store, request=request, provider=ManualSemanticMemoryProvider(_batch())
        )
        return CatalogFileUpdate(result.store, result)

    generated = repository.update(_generate)
    table_id = next(
        p.proposal_id
        for p in generated.store.iter_draft_proposals("demo")
        if p.target.object_type.value == "table"
    )

    def _approve(store: CatalogStore) -> CatalogFileUpdate:
        result = governance.approve_proposal(
            store, proposal_id=table_id, connection_id="demo", actor="reviewer", now=_NOW
        )
        return CatalogFileUpdate(result.store, result)

    repository.update(_approve)

    def _publish(store: CatalogStore) -> CatalogFileUpdate:
        result = governance.publish_proposal(
            store, proposal_id=table_id, connection_id="demo", actor="publisher", now=_NOW
        )
        return CatalogFileUpdate(result.store, result)

    repository.update(_publish)

    reloaded = CatalogStore.from_file(str(catalog_path))
    assert reloaded.get_table("demo", "customers").description == "Customer entity."
    assert reloaded.get_draft_proposal(table_id).review_status == ProposalReviewStatus.PUBLISHED
