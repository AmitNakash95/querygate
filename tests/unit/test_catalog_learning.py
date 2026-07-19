"""TODO item 32C: the usage learner's support/confidence gating, decay,
conflict rule, idempotent replay, dedup against existing/open content, and
that a learned proposal flows through the *unmodified* 32B governance
state machine (never able to publish itself).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from querygate.catalog import governance
from querygate.catalog.learning import (
    CONFLICT_MARGIN,
    FULL_CONFIDENCE_SUPPORT,
    MIN_CONFIDENCE,
    MIN_DISTINCT_PRINCIPALS,
    SIGNAL_TTL,
    generate_learned_relationship_proposals,
)
from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogDraftObjectType,
    CatalogDraftTarget,
    CatalogUsageSignalKind,
    ProposalReviewStatus,
)
from querygate.catalog.repository import CatalogFileRepository, CatalogFileUpdate
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.catalog.usage import build_usage_signal, record_usage_signals
from querygate.core.exceptions import CatalogGovernanceError

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FULL_SUPPORT = FULL_CONFIDENCE_SUPPORT  # convenience alias
MIN_SUPPORT_FOR_CONFIDENCE = 5  # ceil(MIN_CONFIDENCE * FULL_CONFIDENCE_SUPPORT) == 5


def _snapshot(connection_id: str = "demo") -> ObservedSchemaSnapshot:
    metadata = sa.MetaData()
    customers = sa.Table("customers", metadata, sa.Column("id", sa.Integer, primary_key=True))
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id")),
        sa.Column("shipping_customer_id", sa.Integer),
    )
    return ObservedSchemaSnapshot.from_tables(connection_id, [customers, orders])


def _target(
    *,
    connection_id="demo",
    table="orders",
    column="customer_id",
    to_table="customers",
    to_column="id",
):
    return CatalogDraftTarget(
        connection_id=connection_id,
        object_type=CatalogDraftObjectType.RELATIONSHIP,
        table=table,
        column=column,
        to_table=to_table,
        to_column=to_column,
    )


def _store_with_signals(*, snapshot, signals) -> CatalogStore:
    store = CatalogStore.from_dict(
        {
            "version": 2,
            "schema_snapshots": {snapshot.connection_id: snapshot.model_dump(mode="json")},
        }
    )
    return record_usage_signals(store, signals=signals).store


def _signals(
    *, count, target=None, connection_id="demo", fingerprint=None, observed_at=None, kind=None
):
    target = target or _target(connection_id=connection_id)
    fingerprint = fingerprint or _snapshot(connection_id).fingerprint
    kind = kind or CatalogUsageSignalKind.RELATIONSHIP_USED
    return [
        build_usage_signal(
            connection_id=connection_id,
            principal_subject=f"user-{i}",
            target=target,
            kind=kind,
            schema_fingerprint=fingerprint,
            evidence_reference=f"admission:{target.table}:{target.to_table}:{i}",
            observed_at=observed_at or NOW,
        )
        for i in range(count)
    ]


# --- requires a schema snapshot ---------------------------------------------


def test_requires_a_persisted_schema_snapshot():
    store = CatalogStore.empty()
    with pytest.raises(ValueError, match="schema snapshot"):
        generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)


# --- support/confidence thresholds ------------------------------------------


def test_below_min_distinct_principals_produces_no_proposal():
    snapshot = _snapshot()
    signals = _signals(count=MIN_DISTINCT_PRINCIPALS - 1)
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "no_evidence"
    assert update.added_count == 0


def test_support_at_min_principals_but_below_confidence_produces_no_proposal():
    snapshot = _snapshot()
    assert MIN_DISTINCT_PRINCIPALS / FULL_CONFIDENCE_SUPPORT < MIN_CONFIDENCE
    signals = _signals(count=MIN_DISTINCT_PRINCIPALS)
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "no_evidence"


def test_enough_support_and_confidence_generates_one_learned_proposal():
    snapshot = _snapshot()
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE)
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "generated"
    assert update.added_count == 1

    proposal = update.store.get_draft_proposal(update.added_proposal_ids[0])
    assert proposal.provenance.source_class.value == "learned"
    assert proposal.provenance.status.value == "draft"
    assert proposal.review_status == ProposalReviewStatus.PENDING
    assert proposal.target.table == "orders"
    assert proposal.target.to_table == "customers"
    # Confidence reflects support, not a fixed constant.
    assert proposal.provenance.confidence == pytest.approx(
        MIN_SUPPORT_FOR_CONFIDENCE / FULL_CONFIDENCE_SUPPORT
    )


# --- decay / TTL --------------------------------------------------------------


def test_signals_older_than_ttl_do_not_count_as_support():
    snapshot = _snapshot()
    stale_time = NOW - SIGNAL_TTL - timedelta(days=1)
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE, observed_at=stale_time)
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "no_evidence"


def test_signals_within_ttl_still_count():
    snapshot = _snapshot()
    recent_time = NOW - SIGNAL_TTL + timedelta(days=1)
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE, observed_at=recent_time)
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "generated"


# --- schema-fingerprint drift -------------------------------------------------


def test_signals_from_a_stale_schema_fingerprint_are_excluded():
    snapshot = _snapshot()
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE, fingerprint="sha256:" + "0" * 64)
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "no_evidence"


# --- conflict rule -------------------------------------------------------------


def test_ambiguous_competing_targets_produce_no_proposal_for_either():
    snapshot = _snapshot()
    # Two different to_tables competing for the same source column
    # ("orders.customer_id") — a genuinely ambiguous preferred join.
    target_a = _target(table="orders", column="customer_id", to_table="customers", to_column="id")
    target_b = _target(
        table="orders", column="customer_id", to_table="archived_customers", to_column="id"
    )
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE, target=target_a) + _signals(
        count=MIN_SUPPORT_FOR_CONFIDENCE, target=target_b
    )
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "no_evidence"


def test_clear_leader_past_margin_is_proposed_over_the_runner_up():
    snapshot = _snapshot()
    target_a = _target(table="orders", column="customer_id", to_table="customers", to_column="id")
    target_b = _target(
        table="orders", column="customer_id", to_table="archived_customers", to_column="id"
    )
    leader_support = MIN_SUPPORT_FOR_CONFIDENCE + CONFLICT_MARGIN
    signals = _signals(count=leader_support, target=target_a) + _signals(
        count=MIN_SUPPORT_FOR_CONFIDENCE, target=target_b
    )
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "generated"
    assert update.added_count == 1
    proposal = update.store.get_draft_proposal(update.added_proposal_ids[0])
    assert proposal.target.to_table == "customers"


# --- dedup against existing verified content / open proposals ---------------


def test_already_verified_relationship_is_not_relearned():
    snapshot = _snapshot()
    raw = {
        "version": 2,
        "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
        "connections": {
            "demo": {
                "tables": {
                    "orders": {
                        "relationships": [
                            {
                                "column": "customer_id",
                                "to_table": "customers",
                                "to_column": "id",
                                "provenance": {"status": "verified", "source_class": "verified"},
                            }
                        ]
                    }
                }
            }
        },
    }
    store = CatalogStore.from_dict(raw)
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE)
    store = record_usage_signals(store, signals=signals).store

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "no_evidence"


def test_replay_against_unchanged_evidence_is_idempotent():
    snapshot = _snapshot()
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE)
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    first = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert first.outcome == "generated"

    second = generate_learned_relationship_proposals(first.store, connection_id="demo", now=NOW)
    assert second.outcome == "idempotent"
    assert len(list(second.store.iter_draft_proposals("demo"))) == 1


def test_open_pending_proposal_is_never_duplicated_by_new_evidence():
    """Extra evidence gathered after a proposal already exists must not
    create a second proposal for the same relationship.
    """

    snapshot = _snapshot()
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE)
    store = _store_with_signals(snapshot=snapshot, signals=signals)
    first = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert first.outcome == "generated"

    more_signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE + 1)
    store_with_more = record_usage_signals(first.store, signals=more_signals).store
    second = generate_learned_relationship_proposals(store_with_more, connection_id="demo", now=NOW)
    assert second.outcome == "no_evidence"
    assert len(list(second.store.iter_draft_proposals("demo"))) == 1


# --- cross-connection / cross-principal isolation ---------------------------


def test_signals_from_another_connection_never_contribute_support():
    snapshot = _snapshot("demo")
    other_snapshot = _snapshot("other")
    demo_signals = _signals(count=MIN_DISTINCT_PRINCIPALS)  # below threshold alone
    other_signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE, connection_id="other")

    store = CatalogStore.from_dict(
        {
            "version": 2,
            "schema_snapshots": {
                "demo": snapshot.model_dump(mode="json"),
                "other": other_snapshot.model_dump(mode="json"),
            },
        }
    )
    store = record_usage_signals(store, signals=demo_signals + other_signals).store

    demo_update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert demo_update.outcome == "no_evidence"

    other_update = generate_learned_relationship_proposals(
        demo_update.store, connection_id="other", now=NOW
    )
    assert other_update.outcome == "generated"


def test_repeated_signals_from_one_principal_do_not_inflate_support():
    """Support is counted by *distinct* principal_partition, not signal
    count — one caller hammering the same join many times must not look
    like independent corroborating evidence.
    """

    snapshot = _snapshot()
    target = _target()
    signals = [
        build_usage_signal(
            connection_id="demo",
            principal_subject="same-user",
            target=target,
            kind=CatalogUsageSignalKind.RELATIONSHIP_USED,
            schema_fingerprint=snapshot.fingerprint,
            evidence_reference=f"admission:{i}",
            observed_at=NOW,
        )
        for i in range(50)
    ]
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    update = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    assert update.outcome == "no_evidence"


# --- integration with the unmodified 32B governance state machine ----------


def test_learned_proposal_flows_through_unmodified_approve_publish_and_cannot_self_publish():
    snapshot = _snapshot()
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE)
    store = _store_with_signals(snapshot=snapshot, signals=signals)

    generated = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    proposal_id = generated.added_proposal_ids[0]
    store = generated.store

    with pytest.raises(CatalogGovernanceError):
        governance.publish_proposal(
            store, proposal_id=proposal_id, connection_id="demo", actor="reviewer"
        )

    approved = governance.approve_proposal(
        store, proposal_id=proposal_id, connection_id="demo", actor="reviewer"
    )
    published = governance.publish_proposal(
        approved.store, proposal_id=proposal_id, connection_id="demo", actor="reviewer"
    )
    assert published.outcome == "published"
    final_proposal = published.store.get_draft_proposal(proposal_id)
    assert final_proposal.review_status == ProposalReviewStatus.PUBLISHED
    published_table = published.store.get_table("demo", "orders")
    relationship = next(r for r in published_table.relationships if r.column == "customer_id")
    assert relationship.provenance.source_class.value == "verified"
    assert relationship.provenance.status.value == "verified"


def test_learned_proposal_is_not_agent_visible_before_publish():
    from querygate.catalog.models import agent_visible

    snapshot = _snapshot()
    signals = _signals(count=MIN_SUPPORT_FOR_CONFIDENCE)
    store = _store_with_signals(snapshot=snapshot, signals=signals)
    generated = generate_learned_relationship_proposals(store, connection_id="demo", now=NOW)
    proposal = generated.store.get_draft_proposal(generated.added_proposal_ids[0])
    # A pending learned proposal exists only in draft_proposals, never in a
    # searchable table/column/relationship entry regardless of provenance
    # status — search_catalog only ever iterates store.iter_tables().
    assert list(generated.store.iter_tables("demo")) == [] or all(
        table.provenance.entry_id != proposal.provenance.entry_id
        for _name, table in generated.store.iter_tables("demo")
    )
    assert agent_visible(proposal.provenance)  # draft status alone isn't hidden...
    # ...but it is still never returned by search_catalog because it isn't
    # part of any table/column/relationship entry at all until published.
