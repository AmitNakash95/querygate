"""TODO item 32C: turn accumulated usage evidence into quarantined
``learned`` draft proposals — relationship (preferred-join) hints only.

A usage signal tells us *what* a successfully executed, already
policy-validated query touched, not what it means — so this learner never
proposes a description/alias/default_aggregation for a table or column from
usage alone. The one artifact usage can honestly support is "these two
tables are frequently joined this way", so that is the only thing it
proposes, using a single fixed, templated description (never free text
derived from any caller input).

Everything about *how* a proposal becomes real catalog content is
unchanged: this produces a plain ``CatalogDraftProposal``
(``source_class=learned``, ``status=draft``) through the exact same
``draft_proposals``/``generation_records`` lists 32A-2's
``generate_catalog_drafts`` already uses, so it goes through the same 32B
``pending -> approved -> published`` review workflow, can never publish
itself, and is never agent-visible before that (``agent_visible``/
``search_catalog`` already gate on review/provenance status, unchanged).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from querygate.catalog.generation import CatalogGenerationUpdate
from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogDraftContent,
    CatalogDraftObjectType,
    CatalogDraftProposal,
    CatalogDraftTarget,
    CatalogEntryProvenance,
    CatalogEntryStatus,
    CatalogGenerationRecord,
    CatalogUsageSignal,
    CatalogUsageSignalKind,
    ProposalReviewStatus,
)

# Compiled release thresholds — not fixture-tunable, per item 32C's
# "define release thresholds before implementation results are known".
MIN_DISTINCT_PRINCIPALS = 3
FULL_CONFIDENCE_SUPPORT = 8
MIN_CONFIDENCE = 0.6
SIGNAL_TTL = timedelta(days=30)
# A competing target must lead the runner-up by at least this many distinct
# principals before the learner will pick a side; otherwise both competing
# relationships for the same source column are skipped as ambiguous rather
# than guessed (the "conflict rule").
CONFLICT_MARGIN = 2

_RelationshipKey = tuple[str, str, str, str]  # (table, column, to_table, to_column), casefolded


def _relationship_key(target: CatalogDraftTarget) -> _RelationshipKey:
    return (
        target.table.casefold(),
        (target.column or "").casefold(),
        (target.to_table or "").casefold(),
        (target.to_column or "").casefold(),
    )


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _existing_relationship_provenance(
    store: CatalogStore, connection_id: str, target: CatalogDraftTarget
) -> Optional[CatalogEntryProvenance]:
    table = store.get_table(connection_id, target.table)
    if table is None:
        return None
    key = _relationship_key(target)
    for relationship in table.relationships:
        candidate = CatalogDraftTarget(
            connection_id=connection_id,
            object_type=CatalogDraftObjectType.RELATIONSHIP,
            table=target.table,
            column=relationship.column,
            to_table=relationship.to_table,
            to_column=relationship.to_column,
        )
        if _relationship_key(candidate) == key:
            return relationship.provenance
    return None


def _has_open_proposal(store: CatalogStore, connection_id: str, target: CatalogDraftTarget) -> bool:
    """True if a pending/approved proposal already covers this relationship —
    the learner must never duplicate a proposal already awaiting review,
    including across replay or a concurrent second worker.
    """

    key = _relationship_key(target)
    for proposal in store.iter_draft_proposals(connection_id):
        if (
            proposal.target.object_type == CatalogDraftObjectType.RELATIONSHIP
            and _relationship_key(proposal.target) == key
            and proposal.review_status
            in {ProposalReviewStatus.PENDING, ProposalReviewStatus.APPROVED}
        ):
            return True
    return False


def generate_learned_relationship_proposals(
    store: CatalogStore,
    *,
    connection_id: str,
    now: Optional[datetime] = None,
) -> CatalogGenerationUpdate:
    """Turn accumulated ``relationship_used`` evidence into quarantined
    ``learned`` proposals.

    Deterministic and idempotent: the generation id is itself derived from
    the exact evidence snapshot being proposed, so re-running against
    unchanged evidence (replay, or a second concurrent worker) always
    resolves to the same id and is a pure no-op — unlike
    ``generate_catalog_drafts``'s operator-chosen id, there is no
    "same id, different input" case to reject here, because the id *is* a
    function of the input.
    """

    now = now or datetime.now(timezone.utc)
    snapshot = store.get_schema_snapshot(connection_id)
    if snapshot is None:
        raise ValueError("usage-based learning requires a persisted schema snapshot")

    cutoff = now - SIGNAL_TTL
    grouped: dict[_RelationshipKey, list[CatalogUsageSignal]] = defaultdict(list)
    for signal in store.iter_usage_signals(connection_id):
        if signal.kind != CatalogUsageSignalKind.RELATIONSHIP_USED:
            continue
        if signal.schema_fingerprint != snapshot.fingerprint:
            continue  # schema drift invalidates old evidence
        if signal.observed_at < cutoff:
            continue  # decayed out of the support window
        grouped[_relationship_key(signal.target)].append(signal)

    supports = {
        key: len({signal.principal_partition for signal in signals})
        for key, signals in grouped.items()
    }
    targets = {key: signals[0].target for key, signals in grouped.items()}

    by_source_column: dict[tuple[str, str], list[_RelationshipKey]] = defaultdict(list)
    for key in grouped:
        by_source_column[(key[0], key[1])].append(key)

    eligible: list[_RelationshipKey] = []
    for keys in by_source_column.values():
        keys.sort(key=lambda k: -supports[k])
        if len(keys) == 1 or supports[keys[0]] - supports[keys[1]] >= CONFLICT_MARGIN:
            eligible.append(keys[0])
        # else: two or more targets compete for the same source column
        # without a clear leader — skip all of them this cycle rather than
        # guess (the conflict rule).

    # "Qualifying" evidence (support/confidence/conflict-gated) determines the
    # generation id — computed *before* the already-verified/already-open
    # filters below, so a pure replay against byte-identical evidence always
    # resolves to the same id and is caught as idempotent here, regardless of
    # whether an earlier run's proposal is still open. Filtering by
    # already-open/verified first would make the id (and therefore replay
    # detection) depend on unrelated review-workflow state, breaking replay
    # safety for the exact case it exists to protect.
    qualifying: list[tuple[_RelationshipKey, CatalogDraftTarget, int]] = []
    for key in sorted(eligible):
        support = supports[key]
        if support < MIN_DISTINCT_PRINCIPALS:
            continue
        confidence = min(1.0, support / FULL_CONFIDENCE_SUPPORT)
        if confidence < MIN_CONFIDENCE:
            continue
        qualifying.append((key, targets[key], support))

    if not qualifying:
        return CatalogGenerationUpdate(store=store, outcome="no_evidence", generation_id="")

    evidence_snapshot = [
        {
            "table": key[0],
            "column": key[1],
            "to_table": key[2],
            "to_column": key[3],
            "support": support,
        }
        for key, _target, support in qualifying
    ]
    generation_id = (
        "usage-learner:"
        + _fingerprint(
            {
                "connection_id": connection_id,
                "schema_fingerprint": snapshot.fingerprint,
                "relationships": evidence_snapshot,
            }
        )[7:39]
    )

    existing_record = store.get_generation_record(generation_id)
    if existing_record is not None:
        return CatalogGenerationUpdate(
            store=store, outcome="idempotent", generation_id=generation_id
        )

    # Only now filter out targets already known (verified) or already
    # awaiting review under a *different* generation id — new evidence
    # (e.g. more support for the same relationship) must never duplicate an
    # open proposal for the same target.
    selected: list[tuple[_RelationshipKey, CatalogDraftTarget, int]] = []
    for key, target, support in qualifying:
        existing = _existing_relationship_provenance(store, connection_id, target)
        if existing is not None and existing.status == CatalogEntryStatus.VERIFIED:
            continue  # already known; nothing to learn
        if _has_open_proposal(store, connection_id, target):
            continue  # already awaiting review under a different generation; never duplicate
        selected.append((key, target, support))

    if not selected:
        return CatalogGenerationUpdate(store=store, outcome="no_evidence", generation_id="")

    proposals: list[CatalogDraftProposal] = []
    for key, target, support in selected:
        confidence = min(1.0, support / FULL_CONFIDENCE_SUPPORT)
        digest = _fingerprint({"generation_id": generation_id, "relationship": key})[7:39]
        proposal_id = f"urn:querygate:catalog:draft:{digest}"
        proposals.append(
            CatalogDraftProposal(
                proposal_id=proposal_id,
                generation_id=generation_id,
                target=target,
                content=CatalogDraftContent(
                    description=(
                        f"Frequently used join observed across {support} independent callers."
                    )
                ),
                provenance=CatalogEntryProvenance(
                    entry_id=proposal_id,
                    catalog_version=store.version,
                    source_class="learned",
                    source_evidence=[
                        {"kind": "usage", "reference": f"usage:{connection_id}:{generation_id}"}
                    ],
                    confidence=confidence,
                    status="draft",
                    schema_fingerprint=snapshot.fingerprint,
                    created_at=now,
                ),
            )
        )

    proposal_ids = [proposal.proposal_id for proposal in proposals]
    record = CatalogGenerationRecord(
        generation_id=generation_id,
        connection_id=connection_id,
        provider_mode="usage-learner",
        provider_id="usage-learner",
        prompt_template_version="usage-learner-v1",
        schema_fingerprint=snapshot.fingerprint,
        input_fingerprint=_fingerprint(evidence_snapshot),
        proposal_ids=proposal_ids,
        created_at=now,
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
        generation_id=generation_id,
        added_proposal_ids=tuple(proposal_ids),
    )
